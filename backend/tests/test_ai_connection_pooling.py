"""Tests for Phase 2A: AI Gateway Connection Pooling and Client Lifecycle.

Verifies:
1. Reusable HTTP client and connection pooling across model calls.
2. Graceful client teardown via aclose().
3. Event loop boundary safety (re-initialization across loop changes).
4. Injected client lifecycle ownership (not closed when owned by caller).
5. Explicit Limits and Timeout configuration.
6. ModelGateway aclose() deduplication across routes.
7. aclose_model_gateway() cache clearing and resource cleanup.
8. FastAPI lifespan startup and shutdown integration.
"""

import asyncio
import json
import pytest
import httpx
from pydantic import BaseModel, SecretStr

from app.ai.gateway import (
    ModelCapability,
    ModelGateway,
    ModelRequest,
    ModelResult,
    RawCompletion,
)
from app.ai.providers import (
    DEFAULT_LIMITS,
    OpenAICompatibleProvider,
    aclose_model_gateway,
    build_gateway,
    get_model_gateway,
)
from app.core.config import Settings
from app.main import create_app, lifespan


class DummySchema(BaseModel):
    answer: str


def make_mock_handler(calls_list: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls_list.append(request)
        body = {
            "id": f"chatcmpl-{len(calls_list)}",
            "model": "test-model",
            "choices": [{"message": {"content": json.dumps({"answer": "ok"})}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        return httpx.Response(200, json=body)
    return handler


@pytest.mark.anyio
async def test_provider_reuses_client_across_consecutive_calls():
    calls: list[httpx.Request] = []
    transport = httpx.MockTransport(make_mock_handler(calls))
    provider = OpenAICompatibleProvider(
        "groq",
        SecretStr("gsk-test"),
        "test-model",
        timeout_seconds=10.0,
        transport=transport,
    )

    req = ModelRequest(ModelCapability.PROFILE_EXTRACTION, "system", "user")
    schema = DummySchema.model_json_schema()

    # Call 1
    raw1 = await provider.complete(req, schema, 256)
    client1 = provider._client
    assert client1 is not None
    assert not client1.is_closed

    # Call 2
    raw2 = await provider.complete(req, schema, 256)
    client2 = provider._client

    # Verify the EXACT same client instance was reused
    assert client1 is client2
    assert len(calls) == 2
    assert raw1.provider_request_id == "chatcmpl-1"
    assert raw2.provider_request_id == "chatcmpl-2"

    await provider.aclose()
    assert client1.is_closed
    assert provider._client is None


@pytest.mark.anyio
async def test_provider_aclose_and_reopen():
    calls: list[httpx.Request] = []
    transport = httpx.MockTransport(make_mock_handler(calls))
    provider = OpenAICompatibleProvider(
        "groq",
        SecretStr("gsk-test"),
        "test-model",
        timeout_seconds=10.0,
        transport=transport,
    )

    req = ModelRequest(ModelCapability.PROFILE_EXTRACTION, "system", "user")
    schema = DummySchema.model_json_schema()

    await provider.complete(req, schema, 256)
    old_client = provider._client
    assert old_client is not None

    # Close client
    await provider.aclose()
    assert old_client.is_closed
    assert provider._client is None

    # Completing again should lazily recreate a new client
    await provider.complete(req, schema, 256)
    new_client = provider._client
    assert new_client is not None
    assert new_client is not old_client
    assert not new_client.is_closed

    await provider.aclose()
    assert new_client.is_closed


@pytest.mark.anyio
async def test_provider_async_context_manager():
    calls: list[httpx.Request] = []
    transport = httpx.MockTransport(make_mock_handler(calls))
    req = ModelRequest(ModelCapability.PROFILE_EXTRACTION, "system", "user")
    schema = DummySchema.model_json_schema()

    client_ref = None
    async with OpenAICompatibleProvider(
        "gemini",
        SecretStr("gem-test"),
        "test-model",
        timeout_seconds=10.0,
        transport=transport,
    ) as provider:
        await provider.complete(req, schema, 256)
        client_ref = provider._client
        assert client_ref is not None
        assert not client_ref.is_closed

    # Context manager exit should have closed client
    assert client_ref.is_closed
    assert provider._client is None


def test_provider_event_loop_boundary_safety():
    """Verifies that if a provider instance crosses event loops, it safely spawns a fresh client."""
    calls: list[httpx.Request] = []
    transport = httpx.MockTransport(make_mock_handler(calls))
    provider = OpenAICompatibleProvider(
        "groq",
        SecretStr("gsk-test"),
        "test-model",
        timeout_seconds=10.0,
        transport=transport,
    )
    req = ModelRequest(ModelCapability.PROFILE_EXTRACTION, "system", "user")
    schema = DummySchema.model_json_schema()

    # Loop 1
    async def run_in_loop_1():
        await provider.complete(req, schema, 256)
        return provider._client

    client1 = asyncio.run(run_in_loop_1())
    assert client1 is not None

    # Loop 2 - different event loop
    async def run_in_loop_2():
        await provider.complete(req, schema, 256)
        return provider._client

    client2 = asyncio.run(run_in_loop_2())
    assert client2 is not None
    # Client was replaced because event loop changed
    assert client2 is not client1


@pytest.mark.anyio
async def test_injected_client_is_not_closed_by_provider():
    calls: list[httpx.Request] = []
    transport = httpx.MockTransport(make_mock_handler(calls))
    custom_client = httpx.AsyncClient(
        base_url="https://api.groq.com/openai/v1",
        transport=transport,
    )

    provider = OpenAICompatibleProvider(
        "groq",
        SecretStr("gsk-test"),
        "test-model",
        timeout_seconds=10.0,
        client=custom_client,
    )

    req = ModelRequest(ModelCapability.PROFILE_EXTRACTION, "system", "user")
    schema = DummySchema.model_json_schema()
    await provider.complete(req, schema, 256)

    # Provider aclose should NOT close caller-injected client
    await provider.aclose()
    assert not custom_client.is_closed

    await custom_client.aclose()
    assert custom_client.is_closed


@pytest.mark.anyio
async def test_custom_limits_and_timeouts():
    custom_limits = httpx.Limits(max_keepalive_connections=5, max_connections=12, keepalive_expiry=15.0)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={
        "id": "1",
        "choices": [{"message": {"content": json.dumps({"answer": "custom"})}}],
    }))

    provider = OpenAICompatibleProvider(
        "groq",
        SecretStr("gsk-test"),
        "test-model",
        timeout_seconds=8.0,
        connect_timeout_seconds=3.0,
        limits=custom_limits,
        transport=transport,
    )

    req = ModelRequest(ModelCapability.PROFILE_EXTRACTION, "system", "user")
    schema = DummySchema.model_json_schema()
    await provider.complete(req, schema, 256)

    client = provider._client
    assert client is not None
    assert client.timeout.connect == 3.0
    assert client.timeout.read == 8.0
    await provider.aclose()


@pytest.mark.anyio
async def test_gateway_aclose_deduplicates_shared_providers():
    calls: list[httpx.Request] = []
    transport = httpx.MockTransport(make_mock_handler(calls))
    provider = OpenAICompatibleProvider(
        "groq",
        SecretStr("gsk-test"),
        "test-model",
        timeout_seconds=10.0,
        transport=transport,
    )

    # Map multiple capabilities to the exact same provider instance
    gateway = ModelGateway(
        {
            ModelCapability.PROFILE_EXTRACTION: [provider],
            ModelCapability.INTERVIEW_PLANNING: [provider],
            ModelCapability.QUESTION_GENERATION: [provider],
        },
        timeout_seconds=10.0,
        max_retries=0,
        max_output_tokens=256,
    )

    req = ModelRequest(ModelCapability.PROFILE_EXTRACTION, "system", "user")
    schema = DummySchema.model_json_schema()
    await provider.complete(req, schema, 256)
    client = provider._client
    assert client is not None
    assert not client.is_closed

    # gateway.aclose() should close the provider once
    await gateway.aclose()
    assert client.is_closed
    assert provider._client is None


@pytest.mark.anyio
async def test_aclose_model_gateway_clears_cache(monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setenv("AI_PROVIDERS", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-mock")
    monkeypatch.setenv("GROQ_MODEL", "mock-model")
    get_settings.cache_clear()
    get_model_gateway.cache_clear()

    # Calling get_model_gateway caches the gateway
    gw = get_model_gateway()
    assert gw is not None
    assert get_model_gateway.cache_info().currsize == 1

    # aclose_model_gateway closes and clears cache
    await aclose_model_gateway()
    assert get_model_gateway.cache_info().currsize == 0
    get_settings.cache_clear()


@pytest.mark.anyio
async def test_fastapi_lifespan_closes_gateway_on_shutdown(monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setenv("AI_PROVIDERS", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-mock")
    monkeypatch.setenv("GROQ_MODEL", "mock-model")
    get_settings.cache_clear()
    get_model_gateway.cache_clear()

    app = create_app()

    # Pre-populate gateway in cache
    gw = get_model_gateway()
    assert gw is not None
    assert get_model_gateway.cache_info().currsize == 1

    # Simulate lifespan startup and shutdown
    async with lifespan(app):
        # Application running
        assert get_model_gateway.cache_info().currsize == 1

    # After lifespan exits, gateway cache should have been cleared cleanly
    assert get_model_gateway.cache_info().currsize == 0
    get_settings.cache_clear()
