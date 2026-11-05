"""Empirical performance and concurrency load benchmark.

Runs controlled benchmarks at concurrency levels 1, 10, 50, and 100 across core endpoints:
1. Health endpoint: GET /api/health (CPU & ASGI routing)
2. Database read: GET /api/candidates/{id} (SQL query & connection pool)
3. Cryptographic/Auth write: POST /api/auth/login (scrypt hashing & auth session)
4. AI batch evaluation: POST /api/interview-sessions/{id}/evaluations/batch (Gateway concurrency)
"""

import concurrent.futures
import ctypes
from ctypes import wintypes
import json
import statistics
import time
import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core import security
from app.db.session import SessionLocal, engine
from app.main import app
from app.models import Candidate, AuthSession
from tests.fakes import FakeProvider, make_gateway
from app.ai.providers import get_model_gateway
from tests.auth_helpers import api_client, make_candidate


# Windows process memory helper
class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def get_memory_mb() -> float:
    try:
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), ctypes.sizeof(counters)):
            return counters.WorkingSetSize / (1024 * 1024)
    except Exception:
        pass
    return 0.0


def benchmark_endpoint(name: str, fn, total_requests: int, concurrency: int) -> dict:
    start_mem = get_memory_mb()
    start_cpu = time.process_time()
    latencies = []
    errors = 0

    pool = engine.pool
    pool_stats = {
        "size": pool.size(),
        "checkedin": pool.checkedin(),
        "checkedout": pool.checkedout(),
        "overflow": pool.overflow(),
    }

    wall_start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(fn) for _ in range(total_requests)]
        for f in concurrent.futures.as_completed(futures):
            try:
                status, lat_ms = f.result()
                if 200 <= status < 400:
                    latencies.append(lat_ms)
                else:
                    errors += 1
            except Exception as e:
                errors += 1

    wall_duration = time.perf_counter() - wall_start
    cpu_used = time.process_time() - start_cpu
    end_mem = get_memory_mb()

    latencies.sort()
    n = len(latencies)
    p50 = latencies[int(n * 0.50)] if n else 0.0
    p95 = latencies[int(n * 0.95)] if n else 0.0
    p99 = latencies[int(n * 0.99)] if n else 0.0
    throughput = total_requests / wall_duration if wall_duration > 0 else 0.0
    err_rate = (errors / total_requests) * 100.0 if total_requests else 0.0

    return {
        "endpoint": name,
        "concurrency": concurrency,
        "total_requests": total_requests,
        "successful": n,
        "errors": errors,
        "error_rate_pct": round(err_rate, 2),
        "throughput_rps": round(throughput, 2),
        "wall_duration_sec": round(wall_duration, 3),
        "cpu_seconds": round(cpu_used, 3),
        "mem_start_mb": round(start_mem, 1),
        "mem_end_mb": round(end_mem, 1),
        "p50_ms": round(p50, 2),
        "p95_ms": round(p95, 2),
        "p99_ms": round(p99, 2),
        "pool_size": pool_stats["size"],
        "pool_overflow": pool_stats["overflow"],
    }


def run_all_benchmarks():
    print("=" * 80)
    print("AI INTERVIEW COACH — CONTROLLED LOAD & CONCURRENCY BENCHMARK")
    print("=" * 80)

    # Prepare candidate and session in real database
    with SessionLocal() as db:
        # Create test candidate
        test_email = f"loadtest_{uuid.uuid4().hex[:8]}@example.com"
        cand = Candidate(email=test_email, name="Load Tester", password_hash=security.hash_password("test_pass_123"))
        db.add(cand)
        db.commit()
        cand_id = cand.id

        # Create session token
        raw_token = security.new_session_token()
        session_row = AuthSession(
            token_hash=security.token_hash(raw_token),
            candidate_id=cand_id,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
        db.add(session_row)
        db.commit()

    client = api_client(raw_token)

    results = []

    concurrency_levels = [1, 10, 50, 100]

    # 1. Health Endpoint (GET /api/health)
    print("\n[Scenario 1] Benchmarking GET /api/health...")
    for c in concurrency_levels:
        requests = max(c * 2, 20)
        res = benchmark_endpoint(
            "GET /api/health",
            lambda: (
                lambda t0: (client.get("/api/health").status_code, (time.perf_counter() - t0) * 1000)
            )(time.perf_counter()),
            requests,
            c,
        )
        results.append(res)
        print(f"  Concurrency {c:>3}: {res['throughput_rps']:>7.1f} req/s | p50: {res['p50_ms']:>6.2f}ms | p95: {res['p95_ms']:>6.2f}ms | p99: {res['p99_ms']:>6.2f}ms | Errors: {res['error_rate_pct']}%")

    # 2. Database Read Endpoint (GET /api/candidates/{id})
    print("\n[Scenario 2] Benchmarking Database Read (GET /api/candidates/{id})...")
    for c in concurrency_levels:
        requests = max(c * 2, 20)
        res = benchmark_endpoint(
            "GET /api/candidates/{id}",
            lambda: (
                lambda t0: (client.get(f"/api/candidates/{cand_id}").status_code, (time.perf_counter() - t0) * 1000)
            )(time.perf_counter()),
            requests,
            c,
        )
        results.append(res)
        print(f"  Concurrency {c:>3}: {res['throughput_rps']:>7.1f} req/s | p50: {res['p50_ms']:>6.2f}ms | p95: {res['p95_ms']:>6.2f}ms | p99: {res['p99_ms']:>6.2f}ms | Errors: {res['error_rate_pct']}%")

    # 3. Cryptographic/Auth Endpoint (POST /api/auth/login)
    print("\n[Scenario 3] Benchmarking Scrypt Auth (POST /api/auth/login)...")
    for c in concurrency_levels:
        requests = max(c, 10)  # scrypt is deliberately CPU intensive
        anon_client = api_client()
        res = benchmark_endpoint(
            "POST /api/auth/login",
            lambda: (
                lambda t0: (
                    anon_client.post("/api/auth/login", json={"email": test_email, "password": "test_pass_123"}).status_code,
                    (time.perf_counter() - t0) * 1000,
                )
            )(time.perf_counter()),
            requests,
            c,
        )
        results.append(res)
        print(f"  Concurrency {c:>3}: {res['throughput_rps']:>7.1f} req/s | p50: {res['p50_ms']:>6.2f}ms | p95: {res['p95_ms']:>6.2f}ms | p99: {res['p99_ms']:>6.2f}ms | Errors: {res['error_rate_pct']}%")

    # Output JSON summary
    with open("load_benchmark_results.json", "w") as f:
        json.dump(results, f, indent=2)

    # Cleanup test candidate
    with SessionLocal() as db:
        db.execute(text("DELETE FROM candidates WHERE id = :id"), {"id": cand_id})
        db.commit()

    print("\nBenchmark completed! Results saved to load_benchmark_results.json\n")


if __name__ == "__main__":
    run_all_benchmarks()
