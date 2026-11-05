"""Model gateway: routing, structured output, retry/backoff, timeout, fallback, adapter, config."""

import json
import logging
import time

try:
    import httpx
except ImportError:
    import httpx2 as httpx
import pytest
from fakes import HANG, FakeProvider, RecordingSleep, make_gateway
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from app.ai.gateway import (
    ErrorKind,
    ModelCapability,
    ModelGateway,
    ModelNotConfiguredError,
    ModelRequest,
    ModelUnavailableError,
    ProviderError,
    RawCompletion,
)
from app.ai.providers import BASE_URLS, OpenAICompatibleProvider, build_gateway
from app.core.config import Settings
from app.models.resume import FactCategory

pytestmark = pytest.mark.anyio
KEY = "sk-test-SECRET-do-not-log-4242"
OK = {"items": [{"name": "Python", "category": "SKILL"}]}


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    category: FactCategory


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[Item]


REQUEST = ModelRequest(ModelCapability.PROFILE_EXTRACTION, "Extract items.", "<data>x</data>")


# ---------- success + normalised response ----------


async def test_success_returns_validated_output_and_metadata() -> None:
    provider = FakeProvider("groq", OK)
    result = await make_gateway(provider).generate_structured(REQUEST, Out)
    assert result.output == Out(items=[Item(name="Python", category=FactCategory.SKILL)])
    r = result.record
    assert len(r.request_id) == 32
    assert (r.capability, r.requested_provider, r.provider, r.model) == (
        ModelCapability.PROFILE_EXTRACTION, "groq", "groq", "fake-model-1"
    )
    assert (r.attempts, r.fallback_used, r.error_type, r.succeeded) == (1, False, None, True)
    assert (r.input_tokens, r.output_tokens, r.total_tokens) == (120, 30, 150)
    assert (r.provider_request_id, r.finish_reason) == ("groq-req", "stop")
    assert r.latency_ms >= 0


async def test_unreported_usage_stays_none() -> None:
    raw = RawCompletion(text=json.dumps(OK), model="m")
    result = await make_gateway(FakeProvider("groq", raw)).generate_structured(REQUEST, Out)
    assert (result.record.input_tokens, result.record.output_tokens,
            result.record.total_tokens) == (None, None, None)


async def test_max_output_tokens_is_capped_by_configuration() -> None:
    seen = []

    class Spy(FakeProvider):
        async def complete(self, request, json_schema, max_output_tokens):  # type: ignore[override]
            seen.append((json_schema["title"], max_output_tokens))
            return await super().complete(request, json_schema, max_output_tokens)

    gateway = make_gateway(Spy("groq", OK))
    await gateway.generate_structured(
        ModelRequest(ModelCapability.PROFILE_EXTRACTION, "s", "u", max_output_tokens=10**6), Out
    )
    assert seen == [("Out", 4096)]


# ---------- structured output validation ----------


@pytest.mark.parametrize(
    "bad",
    [
        "not json at all",
        '{"items": [',  # partial output
        "",
        "   ",
        {"items": [{"name": "Python"}]},  # missing field
        {"items": [{"name": 42, "category": "SKILL"}]},  # wrong type
        {"items": [{"name": "Python", "category": "GENIUS"}]},  # unknown enum value
        {"items": [{"name": "Python", "category": "SKILL", "approved": True}]},  # extra field
        {"items": [], "score": 10},  # extra top-level field
        {"items": {"name": "Python"}},  # malformed nesting
        [OK],  # wrong top-level type
        "x" * 200_001,
    ],
    ids=["not-json", "partial", "empty", "whitespace", "missing-field", "wrong-type",
         "bad-enum", "extra-field", "extra-top-level", "malformed-nesting", "top-level-list",
         "too-large"],
)
async def test_invalid_output_is_rejected_never_returned(bad: object) -> None:
    provider = FakeProvider("groq", bad)
    with pytest.raises(ModelUnavailableError) as exc:
        await make_gateway(provider, retries=1).generate_structured(REQUEST, Out)
    assert exc.value.record.error_type == ErrorKind.INVALID_OUTPUT
    assert exc.value.record.provider is None
    assert len(provider.calls) == 2  # one bounded retry, then give up


async def test_truncated_output_is_rejected_and_not_retried_on_the_same_provider() -> None:
    # Same request, same token limit: a retry would pay for the same truncation again.
    truncated = RawCompletion(text=json.dumps(OK), model="m", finish_reason="length")
    primary, backup = FakeProvider("groq", truncated), FakeProvider("gemini", OK)
    result = await make_gateway(primary, backup, retries=3).generate_structured(REQUEST, Out)
    assert (len(primary.calls), result.record.provider) == (1, "gemini")
    assert result.record.error_type == ErrorKind.INVALID_OUTPUT


async def test_invalid_output_retry_can_recover() -> None:
    provider = FakeProvider("groq", "{broken", OK)
    result = await make_gateway(provider).generate_structured(REQUEST, Out)
    assert result.record.attempts == 2
    assert result.record.error_type == ErrorKind.INVALID_OUTPUT  # what went wrong on the way


async def test_fenced_json_is_accepted() -> None:
    fenced = f"```json\n{json.dumps(OK)}\n```"
    result = await make_gateway(FakeProvider("groq", fenced)).generate_structured(REQUEST, Out)
    assert result.output.items[0].name == "Python"


# ---------- retry, backoff, timeout ----------


async def test_transient_error_is_retried_with_backoff() -> None:
    sleep = RecordingSleep()
    provider = FakeProvider("groq", ErrorKind.RATE_LIMITED, ErrorKind.SERVER_ERROR, OK)
    result = await make_gateway(provider, retries=2, sleep=sleep).generate_structured(REQUEST, Out)
    assert (result.record.provider, result.record.attempts) == ("groq", 3)
    assert sleep.delays == [0.5, 1.0]


async def test_backoff_is_capped_and_retries_are_bounded() -> None:
    sleep = RecordingSleep()
    provider = FakeProvider("groq", ErrorKind.CONNECTION)
    with pytest.raises(ModelUnavailableError) as exc:
        await make_gateway(provider, retries=5, sleep=sleep).generate_structured(REQUEST, Out)
    assert len(provider.calls) == exc.value.record.attempts == 6
    assert sleep.delays == [0.5, 1.0, 2.0, 4.0, 4.0]


async def test_hanging_provider_is_cut_off_by_the_timeout() -> None:
    provider = FakeProvider("groq", HANG)
    with pytest.raises(ModelUnavailableError) as exc:
        await make_gateway(provider, retries=1, timeout=0.05).generate_structured(REQUEST, Out)
    assert exc.value.record.error_type == ErrorKind.TIMEOUT
    assert len(provider.calls) == 2
    assert exc.value.record.latency_ms < 2000


async def test_deadline_caps_the_whole_call_across_retries_and_providers() -> None:
    slow, spare = FakeProvider("groq", HANG), FakeProvider("gemini", OK)
    gateway = make_gateway(slow, spare, retries=3, timeout=10)
    started = time.monotonic()
    with pytest.raises(ModelUnavailableError) as exc:
        await gateway.generate_structured(REQUEST, Out, deadline=started + 0.2)
    assert time.monotonic() - started < 2  # not 4 x 10 s
    assert (len(slow.calls), spare.calls, exc.value.record.error_type) == (1, [], ErrorKind.TIMEOUT)


async def test_deadline_stops_backoff_and_an_expired_deadline_calls_nothing() -> None:
    flaky, sleep = FakeProvider("groq", ErrorKind.SERVER_ERROR), RecordingSleep()
    gateway = make_gateway(flaky, retries=3, sleep=sleep)
    with pytest.raises(ModelUnavailableError) as exc:  # the backoff (0.5 s) would pass it
        await gateway.generate_structured(REQUEST, Out, deadline=time.monotonic() + 0.3)
    assert (len(flaky.calls), sleep.delays, exc.value.record.error_type) == (
        1, [], ErrorKind.TIMEOUT)
    with pytest.raises(ModelUnavailableError) as exc:
        await gateway.generate_structured(REQUEST, Out, deadline=time.monotonic() - 1)
    assert (len(flaky.calls), exc.value.record.attempts) == (1, 0)
    # A deadline far away changes nothing.
    ok = FakeProvider("groq", OK)
    result = await make_gateway(ok).generate_structured(REQUEST, Out,
                                                        deadline=time.monotonic() + 60)
    assert result.record.attempts == 1


@pytest.mark.parametrize("kind", [ErrorKind.AUTH, ErrorKind.MODEL_UNAVAILABLE,
                                  ErrorKind.BAD_REQUEST, ErrorKind.UNEXPECTED])
async def test_permanent_errors_are_not_retried(kind: ErrorKind) -> None:
    provider = FakeProvider("groq", kind)
    with pytest.raises(ModelUnavailableError):
        await make_gateway(provider, retries=3).generate_structured(REQUEST, Out)
    assert len(provider.calls) == 1


# ---------- fallback ----------


async def test_fallback_is_used_and_reported() -> None:
    primary = FakeProvider("groq", ErrorKind.TIMEOUT)
    backup = FakeProvider("gemini", OK, model="gemini-model")
    result = await make_gateway(primary, backup, retries=1).generate_structured(REQUEST, Out)
    r = result.record
    # Never silent: the caller can see what was asked for and what actually answered.
    assert (r.requested_provider, r.provider, r.model, r.fallback_used) == (
        "groq", "gemini", "gemini-model", True
    )
    assert (len(primary.calls), len(backup.calls), r.attempts) == (2, 1, 3)


async def test_auth_failure_falls_back_immediately_and_names_the_setting(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    primary, backup = FakeProvider("groq", ErrorKind.AUTH), FakeProvider("nvidia", OK)
    result = await make_gateway(primary, backup, retries=3).generate_structured(REQUEST, Out)
    assert (len(primary.calls), result.record.provider) == (1, "nvidia")
    assert "check GROQ_API_KEY" in caplog.text


async def test_all_providers_unavailable_is_a_controlled_failure() -> None:
    a, b = FakeProvider("groq", ErrorKind.SERVER_ERROR), FakeProvider("gemini", ErrorKind.TIMEOUT)
    with pytest.raises(ModelUnavailableError) as exc:
        await make_gateway(a, b, retries=1).generate_structured(REQUEST, Out)
    r = exc.value.record
    assert (r.provider, r.model, r.succeeded, r.attempts, r.error_type) == (
        None, None, False, 4, ErrorKind.TIMEOUT
    )


async def test_unconfigured_capability_is_an_error() -> None:
    gateway = ModelGateway({}, timeout_seconds=1, max_retries=0, max_output_tokens=100)
    with pytest.raises(ModelNotConfiguredError):
        await gateway.generate_structured(REQUEST, Out)


# ---------- configuration ----------


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, database_url="postgresql+psycopg://u:p@h/x", **overrides)


def test_no_providers_means_ai_disabled() -> None:
    assert build_gateway(settings(ai_providers="")) is None


def test_provider_order_defines_primary_and_fallback() -> None:
    gateway = build_gateway(settings(
        ai_providers=" Groq , nvidia", groq_api_key=KEY, groq_model="g",
        nvidia_api_key=KEY, nvidia_model="n",
    ))
    providers = gateway.providers_for(ModelCapability.PROFILE_EXTRACTION)
    assert [(p.name, p.model) for p in providers] == [("groq", "g"), ("nvidia", "n")]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"ai_providers": "openai"}, "unknown provider 'openai'"),
        ({"ai_providers": "groq,groq", "groq_api_key": KEY, "groq_model": "m"}, "twice"),
        ({"ai_providers": "groq", "groq_model": "m"}, "needs GROQ_API_KEY and GROQ_MODEL"),
        ({"ai_providers": "gemini", "gemini_api_key": KEY}, "needs GEMINI_API_KEY and GEMINI_MODEL"),
        ({"ai_timeout_seconds": 0}, "ai_timeout_seconds"),
        ({"ai_max_retries": 50}, "ai_max_retries"),
    ],
    ids=["unknown", "duplicate", "missing-key", "missing-model", "zero-timeout", "retry-cap"],
)
def test_invalid_ai_configuration_fails_fast(overrides: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message) as exc:
        settings(**overrides)
    assert KEY not in str(exc.value)


def test_api_keys_never_print() -> None:
    s = settings(ai_providers="groq", groq_api_key=KEY, groq_model="m")
    provider = build_gateway(s).providers_for(ModelCapability.PROFILE_EXTRACTION)[0]
    for text in (repr(s), str(s), repr(provider), str(s.model_dump())):
        assert KEY not in text


# ---------- OpenAI-compatible adapter over a mock HTTP transport ----------


def completion_body(content: str | None = json.dumps(OK), usage: object = "default") -> dict:
    body = {"id": "chatcmpl-1", "model": "served-model",
            "choices": [{"message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}]}
    if usage == "default":
        body["usage"] = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    elif usage is not None:
        body["usage"] = usage
    return body


def adapter(name: str, handler) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(name, SecretStr(KEY), "my-model", timeout_seconds=5,
                                    transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("name", ["groq", "gemini", "nvidia"])
async def test_adapter_request_shape(name: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=completion_body())

    req = ModelRequest(ModelCapability.PROFILE_EXTRACTION, "INSTRUCTIONS", "<d>DATA</d>", 0.0)
    raw = await adapter(name, handler).complete(req, Out.model_json_schema(), 512)
    sent = seen[0]
    assert str(sent.url) == f"{BASE_URLS[name]}/chat/completions"
    assert sent.headers["authorization"] == f"Bearer {KEY}"
    payload = json.loads(sent.content)
    assert payload["model"] == "my-model"
    # Instructions and untrusted data are separate messages; data never enters the system role.
    assert payload["messages"] == [
        {"role": "system", "content": "INSTRUCTIONS"},
        {"role": "user", "content": "<d>DATA</d>"},
    ]
    assert (payload["temperature"], payload["max_tokens"]) == (0.0, 512)
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["schema"]["title"] == "Out"
    # Only NVIDIA gets its documented switch: Nemotron reasoning blew the timeout when live-tested.
    thinking = {"enable_thinking": False} if name == "nvidia" else None
    assert payload.get("chat_template_kwargs") == thinking
    assert raw == RawCompletion(json.dumps(OK), "served-model", "stop", "chatcmpl-1", 11, 7, 18)


# Live-observed (2026-10-01): unknown model -> 404 on all three; bad key -> 401 on Groq/NVIDIA,
# but 400 "Please pass a valid API key" on Gemini.
GEMINI_BAD_KEY = [{"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                             "status": "INVALID_ARGUMENT"}}]


@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [(401, None, ErrorKind.AUTH), (403, None, ErrorKind.AUTH), (400, GEMINI_BAD_KEY, ErrorKind.AUTH),
     (404, None, ErrorKind.MODEL_UNAVAILABLE), (410, None, ErrorKind.MODEL_UNAVAILABLE),
     (429, None, ErrorKind.RATE_LIMITED),
     (408, None, ErrorKind.TIMEOUT), (500, None, ErrorKind.SERVER_ERROR),
     (503, None, ErrorKind.SERVER_ERROR), (400, None, ErrorKind.BAD_REQUEST),
     (422, None, ErrorKind.BAD_REQUEST),
     (400, {"error": {"message": "Failed to generate JSON", "type": "invalid_request_error",
                      "code": "json_validate_failed"}}, ErrorKind.INVALID_OUTPUT)],
)
async def test_adapter_maps_http_status(status: int, body: object, kind: ErrorKind) -> None:
    body = body or {"error": {"message": f"echo of prompt {KEY}"}}
    provider = adapter("gemini", lambda r: httpx.Response(status, json=body))
    with pytest.raises(Exception) as exc:
        await provider.complete(REQUEST, {}, 100)
    assert exc.value.kind == kind
    assert KEY not in str(exc.value) and "echo" not in str(exc.value)


@pytest.mark.parametrize(
    ("error", "kind"),
    [(httpx.ReadTimeout("slow"), ErrorKind.TIMEOUT), (httpx.ConnectError("down"), ErrorKind.CONNECTION)],
)
async def test_adapter_maps_transport_errors(error: Exception, kind: ErrorKind) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    with pytest.raises(Exception) as exc:
        await adapter("groq", handler).complete(REQUEST, {}, 100)
    assert exc.value.kind == kind


@pytest.mark.parametrize(
    "response",
    [httpx.Response(200, text="<html>gateway error</html>"), httpx.Response(200, json={}),
     httpx.Response(200, json={"choices": []}), httpx.Response(200, json={"choices": [None]})],
    ids=["html", "no-choices", "empty-choices", "null-choice"],
)
async def test_adapter_malformed_body_is_invalid_output(response: httpx.Response) -> None:
    with pytest.raises(Exception) as exc:
        await adapter("groq", lambda r: response).complete(REQUEST, {}, 100)
    assert exc.value.kind == ErrorKind.INVALID_OUTPUT


@pytest.mark.parametrize(
    "usage", [None, {}, {"prompt_tokens": "12", "completion_tokens": -1, "total_tokens": True}]
)
async def test_adapter_never_invents_token_counts(usage: object) -> None:
    provider = adapter("groq", lambda r: httpx.Response(200, json=completion_body(usage=usage)))
    raw = await provider.complete(REQUEST, {}, 100)
    assert (raw.input_tokens, raw.output_tokens, raw.total_tokens) == (None, None, None)


async def test_null_content_becomes_invalid_output_in_the_gateway() -> None:
    s = settings(ai_providers="groq", groq_api_key=KEY, groq_model="m", ai_max_retries=0)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=completion_body(None)))
    with pytest.raises(ModelUnavailableError) as exc:
        await build_gateway(s, transport).generate_structured(REQUEST, Out)
    assert exc.value.record.error_type == ErrorKind.INVALID_OUTPUT


async def test_end_to_end_http_fallback_without_leaking_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.groq.com":
            return httpx.Response(503, text=f"overloaded {KEY}")
        return httpx.Response(200, json=completion_body())

    s = settings(ai_providers="groq,gemini", groq_api_key=KEY, groq_model="g",
                 gemini_api_key=KEY, gemini_model="gm", ai_max_retries=1)
    gateway = build_gateway(s, httpx.MockTransport(handler))
    gateway.sleep = RecordingSleep()
    result = await gateway.generate_structured(REQUEST, Out)
    assert (result.record.requested_provider, result.record.provider) == ("groq", "gemini")
    assert (result.record.attempts, result.record.total_tokens) == (3, 18)
    assert KEY not in caplog.text
    assert "Extract items" not in caplog.text and "<data>" not in caplog.text  # no prompt
    assert "Python" not in caplog.text  # no model output


async def test_gemini_gets_the_schema_without_bounds_and_the_gateway_still_enforces_them() -> None:
    """Live 2026-10-03: Gemini answered the planning schema with 400 "too many states for
    serving". It is sent the same shape without bounds; validation is not loosened."""
    from app.ai.providers import relaxed_schema
    from app.schemas.interview_plan import PlanProposal

    full = PlanProposal.model_json_schema()
    sent: dict[str, dict] = {}

    def handler(name):
        def handle(request: httpx.Request) -> httpx.Response:
            sent[name] = json.loads(request.content)["response_format"]["json_schema"]["schema"]
            return httpx.Response(200, json=completion_body(json.dumps({"topics": []})))
        return handle

    req = ModelRequest(ModelCapability.INTERVIEW_PLANNING, "I", "<d>D</d>", 0.0)
    for name in ("gemini", "groq", "nvidia"):
        await adapter(name, handler(name)).complete(req, full, 512)
    text = json.dumps(sent["gemini"])
    assert not any(k in text for k in ('"maxLength"', '"minItems"', '"maxItems"', '"pattern"'))
    assert sent["gemini"]["$defs"]["TopicProposal"]["required"] == \
        full["$defs"]["TopicProposal"]["required"]  # the shape itself is unchanged
    assert sent["groq"] == sent["nvidia"] == full
    assert relaxed_schema({"properties": {"pattern": {"type": "string", "pattern": "x"}}}) == \
        {"properties": {"pattern": {"type": "string"}}}  # a property named like a keyword stays
    # {"topics": []} breaks the dropped minItems=1: the gateway refuses it all the same.
    gateway = ModelGateway({ModelCapability.INTERVIEW_PLANNING: [adapter("gemini", handler("g2"))]},
                           timeout_seconds=5, max_retries=0, max_output_tokens=512)
    with pytest.raises(ModelUnavailableError) as exc:
        await gateway.generate_structured(req, PlanProposal)
    assert exc.value.record.error_type == ErrorKind.INVALID_OUTPUT


# ---------- Retry-After on 429 (free tiers: several evaluations in a row) ----------


@pytest.mark.parametrize(("header", "seconds"), [("2.5", 2.5), ("0", 0.0), (None, None),
                                                 ("Wed, 21 Oct 2026 07:28:00 GMT", None)])
async def test_adapter_reads_retry_after_on_429(header, seconds) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": header} if header else {})

    req = ModelRequest(ModelCapability.PROFILE_EXTRACTION, "I", "<d>D</d>", 0.0)
    with pytest.raises(ProviderError) as exc:
        await adapter("groq", handler).complete(req, Out.model_json_schema(), 512)
    assert (exc.value.kind, exc.value.retry_after) == (ErrorKind.RATE_LIMITED, seconds)


def limited(after: float) -> ProviderError:
    return ProviderError(ErrorKind.RATE_LIMITED, "HTTP 429", retry_after=after)


async def test_a_short_retry_after_is_waited_out_on_the_same_provider() -> None:
    sleep = RecordingSleep()
    primary, backup = FakeProvider("groq", limited(3.0), OK), FakeProvider("gemini", OK)
    result = await make_gateway(primary, backup, sleep=sleep).generate_structured(REQUEST, Out)
    assert (result.record.provider, sleep.delays, len(backup.calls)) == ("groq", [3.0], 0)


async def test_a_long_retry_after_moves_to_the_next_provider_at_once() -> None:
    sleep = RecordingSleep()
    primary, backup = FakeProvider("groq", limited(3600.0)), FakeProvider("gemini", OK)
    result = await make_gateway(primary, backup, sleep=sleep).generate_structured(REQUEST, Out)
    assert (result.record.provider, sleep.delays, len(primary.calls)) == ("gemini", [], 1)


async def test_a_retry_after_past_the_deadline_still_tries_the_next_provider() -> None:
    import time
    primary, backup = FakeProvider("groq", limited(8.0)), FakeProvider("gemini", OK)
    result = await make_gateway(primary, backup).generate_structured(
        REQUEST, Out, deadline=time.monotonic() + 5)
    assert result.record.provider == "gemini"
