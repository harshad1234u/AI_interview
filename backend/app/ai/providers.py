"""Provider adapters: the only code that knows provider URLs, auth and wire formats.

Groq, Gemini and NVIDIA all document an OpenAI-compatible Chat Completions endpoint with
`response_format` JSON-schema output, so one adapter class serves all three, configured by base
URL. Plain HTTPS via httpx2: no vendor SDKs.

  groq    https://console.groq.com/docs/structured-outputs
  gemini  https://ai.google.dev/gemini-api/docs/openai  (beta; unsupported params are ignored)
  nvidia  https://docs.api.nvidia.com/nim/reference/llm-apis

Provider JSON modes differ (Gemini's layer may ignore the schema, NVIDIA prefers its own
extension), so provider output is never trusted: the gateway validates every response itself.
"""

import asyncio
from functools import lru_cache
from typing import Any

try:
    import httpx
except ImportError:
    import httpx2 as httpx  # type: ignore[no-redef]
from pydantic import SecretStr

from app.ai.gateway import (
    ErrorKind,
    ModelCapability,
    ModelGateway,
    ModelRequest,
    ProviderError,
    RawCompletion,
)
from app.core.config import Settings, get_settings

BASE_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "nvidia": "https://integrate.api.nvidia.com/v1",
}

# Extra request fields per provider, from the model docs and verified live (2026-10-01).
EXTRA_BODY: dict[str, dict[str, Any]] = {
    # Nemotron 3 thinks by default: 28 s and ~880 output tokens for a 7-line resume, over the
    # 20 s timeout. Extraction copies text, it needs no reasoning. Other NVIDIA chat templates
    # ignore unknown kwargs.
    "nvidia": {"chat_template_kwargs": {"enable_thinking": False}},
}


# Gemini refuses schemas with many bounds: "The specified schema produces a constraint that has too
# many states for serving" (HTTP 400, verified live 2026-10-03 on the planning schema). It is sent
# the same shape without length, count and pattern bounds. Nothing is loosened in effect: the
# gateway validates every reply against the full Pydantic schema before anyone sees it.
RELAXED_SCHEMA_PROVIDERS = {"gemini"}
BOUND_KEYS = {"maxLength", "minLength", "maxItems", "minItems", "pattern", "maximum", "minimum",
              "exclusiveMaximum", "exclusiveMinimum", "format"}


def relaxed_schema(schema: Any) -> Any:
    """A copy of a JSON schema without value bounds. Names under "properties" and "$defs" are
    names, not keywords, so a property called "pattern" is kept."""
    if isinstance(schema, list):
        return [relaxed_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for key, value in schema.items():
        if key in ("properties", "$defs"):
            out[key] = {name: relaxed_schema(sub) for name, sub in value.items()}
        elif key not in BOUND_KEYS:
            out[key] = relaxed_schema(value)
    return out


def _status_kind(status: int, body: bytes) -> ErrorKind:
    # Gemini answers a bad key with 400 "Please pass a valid API key" / "API key not valid".
    if status in (401, 403) or (status == 400 and b"valid api key" in body.lower()):
        return ErrorKind.AUTH
    # 404: unknown model, or not available to this account (all three). 410: the model was
    # retired (NVIDIA, live 2026-10-03). Either way a configuration problem, never retried.
    if status in (404, 410):
        return ErrorKind.MODEL_UNAVAILABLE
    if status == 429:
        return ErrorKind.RATE_LIMITED
    # Groq checks the generation against the schema itself and answers 400 json_validate_failed
    # when it does not match: that is invalid output (worth a retry), not a bad request.
    if status == 400 and b"json_validate_failed" in body:
        return ErrorKind.INVALID_OUTPUT
    if status == 408:
        return ErrorKind.TIMEOUT
    if status >= 500:
        return ErrorKind.SERVER_ERROR
    return ErrorKind.BAD_REQUEST


def _retry_after(res: httpx.Response) -> float | None:
    """Retry-After in seconds (Groq sends it on 429); an HTTP date or anything odd is ignored."""
    try:
        seconds = float(res.headers.get("retry-after", ""))
    except ValueError:
        return None
    return seconds if 0 <= seconds < 86_400 else None


def _count(value: Any) -> int | None:
    # Reported usage only: anything missing or odd stays None rather than being guessed.
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


DEFAULT_LIMITS = httpx.Limits(
    max_keepalive_connections=20,
    max_connections=50,
    keepalive_expiry=30.0,
)


class OpenAICompatibleProvider:
    def __init__(
        self,
        name: str,
        api_key: SecretStr,
        model: str,
        *,
        timeout_seconds: float,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
        limits: httpx.Limits | None = None,
        connect_timeout_seconds: float = 5.0,
    ) -> None:
        self.name, self.model = name, model
        self._api_key = api_key  # SecretStr: prints as '**********'
        self._base_url = base_url or BASE_URLS[name]
        self._timeout = timeout_seconds
        self._connect_timeout = min(connect_timeout_seconds, timeout_seconds)
        self._transport = transport
        self._limits = limits or DEFAULT_LIMITS
        self._client: httpx.AsyncClient | None = client
        self._client_loop: asyncio.AbstractEventLoop | None = None
        self._owns_client: bool = client is None

    def __repr__(self) -> str:
        return f"OpenAICompatibleProvider({self.name!r}, model={self.model!r})"

    def _get_client(self) -> httpx.AsyncClient:
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if (
            self._client is None
            or self._client.is_closed
            or (self._owns_client and self._client_loop is not None and self._client_loop != current_loop)
        ):
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._timeout, connect=self._connect_timeout),
                limits=self._limits,
                transport=self._transport,
            )
            self._client_loop = current_loop
            self._owns_client = True
        elif self._client_loop is None and current_loop is not None and self._owns_client:
            self._client_loop = current_loop

        return self._client

    async def aclose(self) -> None:
        """Closes the underlying HTTP client cleanly if owned."""
        if self._client is not None and not self._client.is_closed and self._owns_client:
            await self._client.aclose()
        self._client = None
        self._client_loop = None

    async def __aenter__(self) -> "OpenAICompatibleProvider":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()

    async def complete(
        self, request: ModelRequest, json_schema: dict[str, Any], max_output_tokens: int
    ) -> RawCompletion:
        payload = {
            "model": self.model,
            # Trusted instructions and (delimited) untrusted content stay in separate messages.
            "messages": [
                {"role": "system", "content": request.system_instruction},
                {"role": "user", "content": request.user_content},
            ],
            "temperature": request.temperature,
            "max_tokens": max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": (
                    relaxed_schema(json_schema) if self.name in RELAXED_SCHEMA_PROVIDERS
                    else json_schema)},
            },
            **EXTRA_BODY.get(self.name, {}),
        }
        headers = {"Authorization": f"Bearer {self._api_key.get_secret_value()}"}
        client = self._get_client()
        try:
            res = await client.post("/chat/completions", json=payload, headers=headers)
        except httpx.TimeoutException:
            raise ProviderError(ErrorKind.TIMEOUT) from None
        except httpx.TransportError:
            raise ProviderError(ErrorKind.CONNECTION) from None
        if res.status_code != 200:
            # Status code only: provider error bodies can echo the request content.
            raise ProviderError(_status_kind(res.status_code, res.content), f"HTTP {res.status_code}",
                                retry_after=_retry_after(res) if res.status_code == 429 else None)
        try:
            body = res.json()
            choice = body["choices"][0]
            text = choice["message"].get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ProviderError(ErrorKind.INVALID_OUTPUT, "malformed_response") from None
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return RawCompletion(
            text=text if isinstance(text, str) else "",
            model=body.get("model") if isinstance(body.get("model"), str) else self.model,
            finish_reason=choice.get("finish_reason"),
            provider_request_id=body.get("id") if isinstance(body.get("id"), str) else None,
            input_tokens=_count(usage.get("prompt_tokens")),
            output_tokens=_count(usage.get("completion_tokens")),
            total_tokens=_count(usage.get("total_tokens")),
        )


def build_gateway(
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
    client: httpx.AsyncClient | None = None,
) -> ModelGateway | None:
    """None when AI_PROVIDERS is empty: callers then keep the deterministic path only."""
    limits = httpx.Limits(
        max_keepalive_connections=settings.ai_pool_max_keepalive,
        max_connections=settings.ai_pool_max_connections,
        keepalive_expiry=settings.ai_pool_keepalive_expiry,
    )
    providers = [
        OpenAICompatibleProvider(
            name,
            getattr(settings, f"{name}_api_key"),
            getattr(settings, f"{name}_model"),
            timeout_seconds=settings.ai_timeout_seconds,
            transport=transport,
            client=client,
            limits=limits,
            connect_timeout_seconds=settings.ai_connect_timeout_seconds,
        )
        for name in settings.ai_provider_list
    ]
    if not providers:
        return None
    # Capability routing: every capability currently uses the configured provider order.
    return ModelGateway(
        {capability: providers for capability in ModelCapability},
        timeout_seconds=settings.ai_timeout_seconds,
        max_retries=settings.ai_max_retries,
        max_output_tokens=settings.ai_max_output_tokens,
    )


@lru_cache
def get_model_gateway() -> ModelGateway | None:
    return build_gateway(get_settings())


async def aclose_model_gateway() -> None:
    """Closes cached gateway resources and clears the lru_cache."""
    info = get_model_gateway.cache_info()
    if info.currsize > 0:
        gateway = get_model_gateway()
        if gateway is not None and hasattr(gateway, "aclose"):
            await gateway.aclose()
    get_model_gateway.cache_clear()
