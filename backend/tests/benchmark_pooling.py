"""Phase 2A Verification Benchmark: AI Gateway Connection Pooling vs Unpooled.

Measures:
1. MockTransport synthetic benchmark (50 sequential requests):
   - Unpooled (creates/closes httpx.AsyncClient each call)
   - Pooled (reuses httpx.AsyncClient via keepalive pool)
2. Live HTTPS Connection Benchmark (3 requests per provider):
   - Measures TCP + TLS handshake overhead eliminated by persistent keep-alive.
"""

import asyncio
import json
import time
import httpx
from pydantic import BaseModel, SecretStr

from app.ai.gateway import ModelCapability, ModelGateway, ModelRequest
from app.ai.providers import OpenAICompatibleProvider, build_gateway
from app.core.config import get_settings


class BenchmarkSchema(BaseModel):
    message: str


async def run_mock_benchmark():
    payload_response = {
        "id": "chatcmpl-bench",
        "choices": [{"message": {"content": json.dumps({"message": "bench_ok"})}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=payload_response))
    req = ModelRequest(ModelCapability.PROFILE_EXTRACTION, "system", "user")
    schema = BenchmarkSchema.model_json_schema()

    # 1. Unpooled: 50 requests creating and destroying an AsyncClient each time
    unpooled_latencies = []
    t0 = time.perf_counter()
    for _ in range(50):
        t_req = time.perf_counter()
        async with httpx.AsyncClient(base_url="https://api.groq.com/openai/v1", transport=transport) as client:
            res = await client.post("/chat/completions", json={"test": 1})
            assert res.status_code == 200
        unpooled_latencies.append((time.perf_counter() - t_req) * 1000)
    unpooled_total = (time.perf_counter() - t0) * 1000

    # 2. Pooled: 50 requests reusing OpenAICompatibleProvider with pooled client
    provider = OpenAICompatibleProvider(
        "groq",
        SecretStr("gsk-test"),
        "test-model",
        timeout_seconds=10.0,
        transport=transport,
    )
    pooled_latencies = []
    t0 = time.perf_counter()
    for _ in range(50):
        t_req = time.perf_counter()
        raw = await provider.complete(req, schema, 256)
        assert raw.text is not None
        pooled_latencies.append((time.perf_counter() - t_req) * 1000)
    pooled_total = (time.perf_counter() - t0) * 1000
    await provider.aclose()

    avg_unpooled = sum(unpooled_latencies) / len(unpooled_latencies)
    avg_pooled = sum(pooled_latencies) / len(pooled_latencies)

    print("--- MOCK TRANSPORT BENCHMARK (50 iterations) ---")
    print(f"Unpooled Total: {unpooled_total:.2f} ms | Avg per call: {avg_unpooled:.2f} ms")
    print(f"Pooled Total:   {pooled_total:.2f} ms | Avg per call: {avg_pooled:.2f} ms")
    print(f"Speedup:        {avg_unpooled / avg_pooled:.2f}x")


async def run_live_endpoint_benchmark():
    settings = get_settings()
    print("\n--- LIVE HTTPS ENDPOINT HANDSHAKE BENCHMARK ---")
    # Test real TLS endpoints with and without pooling
    endpoints = [
        ("Groq", "https://api.groq.com/openai/v1/models"),
        ("Gemini", "https://generativelanguage.googleapis.com/v1beta/openai/models"),
    ]

    for name, url in endpoints:
        print(f"\nTarget: {name} ({url})")
        # Unpooled: new client every request
        unpooled_times = []
        for i in range(3):
            t0 = time.perf_counter()
            async with httpx.AsyncClient() as client:
                try:
                    await client.get(url, timeout=5.0)
                except Exception:
                    pass
            unpooled_times.append((time.perf_counter() - t0) * 1000)

        # Pooled: single client with connection reuse
        pooled_times = []
        async with httpx.AsyncClient(limits=httpx.Limits(max_keepalive_connections=5, max_connections=10)) as client:
            for i in range(3):
                t0 = time.perf_counter()
                try:
                    await client.get(url, timeout=5.0)
                except Exception:
                    pass
                pooled_times.append((time.perf_counter() - t0) * 1000)

        print(f"  Unpooled Requests (ms): {[round(t, 1) for t in unpooled_times]}")
        print(f"  Pooled Requests   (ms): {[round(t, 1) for t in pooled_times]}")
        first_call = pooled_times[0]
        subsequent_avg = sum(pooled_times[1:]) / len(pooled_times[1:])
        unpooled_avg = sum(unpooled_times) / len(unpooled_times)
        saved_ms = unpooled_avg - subsequent_avg
        print(f"  Initial Handshake Latency: {first_call:.1f} ms")
        print(f"  Warm Kept-Alive Latency:   {subsequent_avg:.1f} ms")
        print(f"  TLS Handshake Elimination: ~{saved_ms:.1f} ms saved per request ({unpooled_avg / subsequent_avg:.2f}x speedup)")


async def main():
    await run_mock_benchmark()
    await run_live_endpoint_benchmark()


if __name__ == "__main__":
    asyncio.run(main())
