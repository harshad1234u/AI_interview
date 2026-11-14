"""Provider-independent model gateway.

Agents say WHAT they need (a capability + a Pydantic schema); the gateway decides WHERE it runs,
enforces timeouts, retries transient failures, falls back to the next provider, and validates the
output before anyone sees it. Agents never import provider code.

The gateway only returns validated data. It never touches the database or application state:
callers decide what (if anything) to do with the output.
"""

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Generic, Protocol, TypeVar

import anyio
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

MAX_OUTPUT_CHARS = 200_000  # hard cap on raw text accepted from a provider
# A provider's Retry-After up to this is waited out (a per-minute limit: free tiers hit it when
# several answers are evaluated in a row); a longer one (a daily quota) moves on at once.
RATE_LIMIT_WAIT_MAX = 10.0


class ModelCapability(StrEnum):
    # Only what an agent actually uses today. Later phases add their own.
    PROFILE_EXTRACTION = "PROFILE_EXTRACTION"
    INTERVIEW_PLANNING = "INTERVIEW_PLANNING"
    QUESTION_GENERATION = "QUESTION_GENERATION"
    ANSWER_EVALUATION = "ANSWER_EVALUATION"


class ErrorKind(StrEnum):
    TIMEOUT = "TIMEOUT"
    CONNECTION = "CONNECTION"
    RATE_LIMITED = "RATE_LIMITED"
    SERVER_ERROR = "SERVER_ERROR"
    INVALID_OUTPUT = "INVALID_OUTPUT"  # empty, truncated, not JSON, or fails the schema
    AUTH = "AUTH"  # bad/unauthorised API key: a configuration problem
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"  # unknown model, or not available to this account
    BAD_REQUEST = "BAD_REQUEST"  # e.g. unsupported parameter such as the response_format
    UNEXPECTED = "UNEXPECTED"


# Worth another try on the same provider. Everything else moves straight to the next provider.
RETRYABLE = {ErrorKind.TIMEOUT, ErrorKind.CONNECTION, ErrorKind.RATE_LIMITED,
             ErrorKind.SERVER_ERROR, ErrorKind.INVALID_OUTPUT}


class ProviderError(Exception):
    """Raised by adapters. The message is a short code, never provider or user content."""

    def __init__(self, kind: ErrorKind, detail: str = "", retry_after: float | None = None) -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind, self.detail = kind, detail
        self.retry_after = retry_after  # seconds the provider asked us to wait (429), if it said


@dataclass(frozen=True)
class ModelRequest:
    capability: ModelCapability
    system_instruction: str  # trusted, written by the application
    user_content: str  # may contain untrusted data; the agent must delimit it
    temperature: float = 0.0
    max_output_tokens: int | None = None  # None = the gateway's configured maximum


@dataclass(frozen=True)
class RawCompletion:
    """What an adapter returns: provider-neutral, unparsed."""

    text: str
    model: str
    finish_reason: str | None = None
    provider_request_id: str | None = None
    input_tokens: int | None = None  # None when the provider doesn't report it; never guessed
    output_tokens: int | None = None
    total_tokens: int | None = None


class ModelProvider(Protocol):
    name: str
    model: str

    async def complete(
        self, request: ModelRequest, json_schema: dict[str, Any], max_output_tokens: int
    ) -> RawCompletion: ...


@dataclass
class CallRecord:
    """Operational metadata about one gateway call. Contains no prompt or output content."""

    request_id: str
    capability: ModelCapability
    requested_provider: str | None
    provider: str | None = None  # the provider that answered (None if none did)
    model: str | None = None
    attempts: int = 0
    fallback_used: bool = False
    error_type: ErrorKind | None = None  # last error seen (also set on success after retries)
    latency_ms: int = 0
    provider_request_id: str | None = None
    finish_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    @property
    def succeeded(self) -> bool:
        return self.provider is not None


@dataclass
class ModelResult(Generic[T]):
    output: T
    record: CallRecord


class ModelUnavailableError(Exception):
    """Every configured provider failed. No output is fabricated."""

    def __init__(self, record: CallRecord) -> None:
        super().__init__(f"model call {record.request_id} failed ({record.error_type})")
        self.record = record


class ModelNotConfiguredError(Exception):
    pass


def parse_structured(raw: RawCompletion, schema: type[T]) -> T:
    """raw provider text -> JSON -> Pydantic. Anything off is INVALID_OUTPUT, never a guess."""
    if raw.finish_reason == "length":
        raise ProviderError(ErrorKind.INVALID_OUTPUT, "truncated")
    text = raw.text.strip()
    if text.startswith("```"):  # some models fence JSON even in JSON mode
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    if not text:
        raise ProviderError(ErrorKind.INVALID_OUTPUT, "empty")
    if len(text) > MAX_OUTPUT_CHARS:
        raise ProviderError(ErrorKind.INVALID_OUTPUT, "too_large")
    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        reason = "not_json" if exc.errors()[0]["type"] == "json_invalid" else "schema"
        raise ProviderError(ErrorKind.INVALID_OUTPUT, reason) from None


class ModelGateway:
    def __init__(
        self,
        routes: dict[ModelCapability, list[ModelProvider]],
        *,
        timeout_seconds: float,
        max_retries: int,
        max_output_tokens: int,
        backoff_base: float = 0.5,
        backoff_max: float = 4.0,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    ) -> None:
        self.routes = routes
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.max_output_tokens = max_output_tokens
        self.backoff_base, self.backoff_max = backoff_base, backoff_max
        self.sleep = sleep

    def providers_for(self, capability: ModelCapability) -> list[ModelProvider]:
        return self.routes.get(capability, [])

    async def generate_structured(
        self, request: ModelRequest, schema: type[T], *, deadline: float | None = None
    ) -> ModelResult[T]:
        """Worst case duration: providers × (max_retries + 1) × timeout, plus backoff.

        `deadline` (an absolute time.monotonic() value) caps the whole call: no attempt or backoff
        runs past it, and an attempt still running then is cut off as a TIMEOUT. None = no cap
        beyond the per-attempt timeout.
        """
        providers = self.providers_for(request.capability)
        if not providers:
            raise ModelNotConfiguredError(request.capability)
        json_schema = schema.model_json_schema()
        max_tokens = min(request.max_output_tokens or self.max_output_tokens, self.max_output_tokens)
        record = CallRecord(uuid.uuid4().hex, request.capability, providers[0].name)
        started = time.monotonic()
        out_of_time = False

        for position, provider in enumerate(providers):
            for retry in range(self.max_retries + 1):
                timeout = self.timeout_seconds
                if deadline is not None:
                    timeout = min(timeout, deadline - time.monotonic())
                if timeout <= 0:
                    out_of_time = True
                    break
                record.attempts += 1
                attempt_started = time.monotonic()
                retry_after: float | None = None
                try:
                    with anyio.fail_after(timeout):
                        raw = await provider.complete(request, json_schema, max_tokens)
                    output = parse_structured(raw, schema)
                except ProviderError as exc:
                    kind, detail, retry_after = exc.kind, exc.detail, exc.retry_after
                except TimeoutError:
                    kind, detail = ErrorKind.TIMEOUT, ""
                except Exception as exc:  # adapter bug: log the class only, try the next provider
                    kind, detail = ErrorKind.UNEXPECTED, type(exc).__name__
                else:
                    record.provider, record.model = provider.name, raw.model
                    record.fallback_used = position > 0
                    record.provider_request_id = raw.provider_request_id
                    record.finish_reason = raw.finish_reason
                    record.input_tokens, record.output_tokens = raw.input_tokens, raw.output_tokens
                    record.total_tokens = raw.total_tokens
                    record.latency_ms = round((time.monotonic() - started) * 1000)
                    self._log(record, provider, "ok", attempt_started)
                    return ModelResult(output, record)

                record.error_type = kind
                self._log(record, provider, f"{kind}{f' ({detail})' if detail else ''}",
                          attempt_started)
                # Same request, same token limit: a truncated answer would just be truncated again.
                if kind not in RETRYABLE or detail == "truncated" or retry == self.max_retries:
                    break
                if retry_after is not None and retry_after > RATE_LIMIT_WAIT_MAX:
                    break  # e.g. a daily quota: try the next provider now
                backoff = min(self.backoff_base * 2**retry, self.backoff_max)
                if retry_after is not None:
                    backoff = max(backoff, retry_after)
                if deadline is not None and time.monotonic() + backoff >= deadline:
                    if retry_after is not None:
                        break  # this provider can't answer in time; another one may
                    out_of_time = True
                    break
                await self.sleep(backoff)
            if out_of_time:  # the caller's deadline: no further attempt on any provider
                record.error_type = ErrorKind.TIMEOUT
                break

        record.latency_ms = round((time.monotonic() - started) * 1000)
        logger.error("Model call %s failed on every provider after %d attempts (last: %s)",
                     record.request_id, record.attempts, record.error_type)
        raise ModelUnavailableError(record)

    @staticmethod
    def _log(record: CallRecord, provider: ModelProvider, outcome: str, started: float) -> None:
        # Metadata only: never prompts, resume text, model output or keys.
        logger.info(
            "Model call %s capability=%s provider=%s model=%s attempt=%d outcome=%s latency_ms=%d "
            "tokens=%s",
            record.request_id, record.capability, provider.name, provider.model, record.attempts,
            outcome, round((time.monotonic() - started) * 1000), record.total_tokens,
        )
        if outcome.startswith(ErrorKind.AUTH):
            logger.error("Provider %s rejected its API key: check %s_API_KEY",
                         provider.name, provider.name.upper())

    async def aclose(self) -> None:
        """Closes all underlying provider clients cleanly."""
        seen: set[Any] = set()
        for providers in self.routes.values():
            for provider in providers:
                if provider not in seen:
                    seen.add(provider)
                    if hasattr(provider, "aclose") and callable(provider.aclose):
                        await provider.aclose()

    async def __aenter__(self) -> "ModelGateway":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()
