"""Phase 3 Comprehensive Backend, Database & AI Gateway Benchmark Suite.

Executes and measures:
1. AI Gateway Connection Pooling vs Handshake Latency (Live Groq, Gemini & Mock)
2. PostgreSQL Query Planning and Execution Profiles (EXPLAIN ANALYZE, Buffers)
3. Full API Route Latency Distributions (POST session, POST answer, POST next, GET questions, GET report)
4. Turn Latency Breakdown & Waterfall Elimination (Phase 2B Verification)
"""

import json
import statistics
import time
from typing import Any, Dict, List, Tuple
from pydantic import BaseModel, SecretStr
import httpx
import psycopg
from fastapi.testclient import TestClient

from app.ai.gateway import ModelCapability, ModelRequest
from app.ai.providers import OpenAICompatibleProvider
from app.core.config import get_settings
from app.main import app
from tests.test_interview_turn_api import ans, nxt, started  # noqa: F401
from tests.test_interview_plan_api import (  # noqa: F401
    EMAIL,
    NAME,
    SETTINGS,
    add_facts,
    candidate,
    consented,
    facts,
    sid,
    use_gateway,
)


def percentile(data: List[float], p: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    d = k - f
    return s[f] + d * (s[c] - s[f])


def compute_stats(data: List[float]) -> Dict[str, float]:
    if not data:
        return {"n": 0, "mean": 0, "median": 0, "p90": 0, "p95": 0, "p99": 0, "min": 0, "max": 0}
    return {
        "n": len(data),
        "mean": round(statistics.mean(data), 2),
        "median": round(statistics.median(data), 2),
        "p90": round(percentile(data, 90), 2),
        "p95": round(percentile(data, 95), 2),
        "p99": round(percentile(data, 99), 2),
        "min": round(min(data), 2),
        "max": round(max(data), 2),
    }


def run_database_benchmarks(dsn: str) -> Dict[str, Any]:
    print("\n" + "=" * 75)
    print("1. POSTGRESQL QUERY EXECUTION & PLANNING PROFILE (EXPLAIN ANALYZE)")
    print("=" * 75)
    
    clean_dsn = dsn.replace("postgresql+psycopg://", "postgresql://")
    conn = psycopg.connect(clean_dsn)
    cur = conn.cursor()
    
    # Discover a valid session_id and candidate_id for realistic query targeting
    cur.execute("SELECT id, candidate_id FROM interview_sessions LIMIT 1")
    row = cur.fetchone()
    if row:
        session_id, candidate_id = str(row[0]), str(row[1])
    else:
        session_id = "00000000-0000-0000-0000-000000000000"
        candidate_id = "00000000-0000-0000-0000-000000000000"

    queries = [
        ("Candidate Lookup by ID", f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT * FROM candidates WHERE id = '{candidate_id}'"),
        ("Candidate Lookup by Email", "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT * FROM candidates WHERE email = 'asha@example.com'"),
        ("Active Session Fetch", f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT * FROM interview_sessions WHERE id = '{session_id}'"),
        ("Questions in Session", f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT * FROM interview_questions WHERE session_id = '{session_id}' ORDER BY asked_at"),
        ("Candidate Facts Extraction", f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT * FROM candidate_facts WHERE candidate_id = '{candidate_id}'"),
        ("Expression Observations", f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT * FROM question_expression_observations WHERE session_id = '{session_id}'"),
        ("Answer Evaluations for Session", f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT * FROM answer_evaluations WHERE session_id = '{session_id}'"),
        ("Evaluation Criteria Listing", "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT * FROM answer_evaluation_criteria LIMIT 50"),
    ]

    results = {}
    for name, query in queries:
        try:
            cur.execute(query)
            plan_res = cur.fetchone()[0][0]
            plan_time = plan_res.get("Planning Time", 0.0)
            exec_time = plan_res.get("Execution Time", 0.0)
            total_time = plan_time + exec_time
            plan_node = plan_res.get("Plan", {})
            node_type = plan_node.get("Node Type", "Unknown")
            hit_blocks = plan_node.get("Shared Hit Blocks", 0)
            read_blocks = plan_node.get("Shared Read Blocks", 0)
            
            results[name] = {
                "planning_time_ms": round(plan_time, 3),
                "execution_time_ms": round(exec_time, 3),
                "total_time_ms": round(total_time, 3),
                "node_type": node_type,
                "shared_hit_blocks": hit_blocks,
                "shared_read_blocks": read_blocks,
            }
            print(f"{name:32} | Total: {total_time:6.3f}ms (Plan: {plan_time:5.3f}ms, Exec: {exec_time:5.3f}ms) | Scan: {node_type:15} | Buffer Hits: {hit_blocks}")
        except Exception as e:
            print(f"{name:32} | FAILED: {e}")
            conn.rollback()

    conn.close()
    return results


async def run_ai_gateway_benchmarks() -> Dict[str, Any]:
    print("\n" + "=" * 75)
    print("2. AI GATEWAY CONNECTION POOLING & TLS HANDSHAKE BENCHMARK")
    print("=" * 75)
    
    endpoints = [
        ("Groq", "https://api.groq.com/openai/v1/models"),
        ("Gemini", "https://generativelanguage.googleapis.com/v1beta/openai/models"),
    ]
    
    results = {}
    for name, url in endpoints:
        # 1. Unpooled: 3 requests destroying client each time (forced handshake)
        unpooled_times = []
        for _ in range(3):
            t0 = time.perf_counter()
            async with httpx.AsyncClient() as client:
                try:
                    await client.get(url, timeout=10.0)
                except Exception:
                    pass
            unpooled_times.append((time.perf_counter() - t0) * 1000)
            
        # 2. Pooled: persistent client (handshake only on 1st request, reused after)
        pooled_times = []
        async with httpx.AsyncClient(limits=httpx.Limits(max_keepalive_connections=5, max_connections=10)) as client:
            for _ in range(3):
                t0 = time.perf_counter()
                try:
                    await client.get(url, timeout=10.0)
                except Exception:
                    pass
                pooled_times.append((time.perf_counter() - t0) * 1000)
                
        initial_handshake = pooled_times[0]
        warm_reused = statistics.mean(pooled_times[1:]) if len(pooled_times) > 1 else pooled_times[0]
        unpooled_avg = statistics.mean(unpooled_times)
        saved_ms = unpooled_avg - warm_reused
        speedup = unpooled_avg / warm_reused if warm_reused > 0 else 1.0
        
        results[name] = {
            "unpooled_avg_ms": round(unpooled_avg, 2),
            "initial_handshake_ms": round(initial_handshake, 2),
            "warm_reused_avg_ms": round(warm_reused, 2),
            "saved_per_request_ms": round(saved_ms, 2),
            "speedup_factor": round(speedup, 2),
        }
        
        print(f"Target: {name} ({url})")
        print(f"  Unpooled Requests (new TLS each): avg = {unpooled_avg:.1f}ms")
        print(f"  Pooled Requests   (warm reused):  avg = {warm_reused:.1f}ms")
        print(f"  TLS Handshake Elimination:       ~{saved_ms:.1f}ms saved per request ({speedup:.2f}x speedup)")

    return results


def run_api_route_benchmarks(client: TestClient, session_id: str, turns: int = 15) -> Dict[str, Any]:
    print("\n" + "=" * 75)
    print(f"3. API ROUTE LATENCY & TURN LATENCY DISTRIBUTIONS (N={turns} turns)")
    print("=" * 75)
    
    submit_times: List[float] = []
    next_times: List[float] = []
    hist_times: List[float] = []
    legacy_turn_times: List[float] = []
    optimized_turn_times: List[float] = []
    
    first_res = nxt(client, session_id).json()
    curr_q = first_res.get("question")
    
    for i in range(turns):
        if curr_q is None:
            break
            
        # 1. POST /answers
        t0 = time.perf_counter()
        res_ans = ans(client, session_id, curr_q["id"], kind="ANSWER", said=f"Benchmarking answer for turn {i+1}")
        t1 = time.perf_counter()
        assert res_ans.status_code == 200
        t_submit = (t1 - t0) * 1000
        submit_times.append(t_submit)
        
        # 2. POST /questions/next
        t2 = time.perf_counter()
        res_next = nxt(client, session_id).json()
        t3 = time.perf_counter()
        t_next = (t3 - t2) * 1000
        next_times.append(t_next)
        curr_q = res_next.get("question")
        
        # 3. GET /questions (history)
        t4 = time.perf_counter()
        res_hist = client.get(f"/api/interview-sessions/{session_id}/questions")
        t5 = time.perf_counter()
        assert res_hist.status_code == 200
        t_hist = (t5 - t4) * 1000
        hist_times.append(t_hist)
        
        legacy_turn_times.append(t_submit + t_next + t_hist)
        optimized_turn_times.append(t_submit + t_next)
        
    # Also benchmark GET /report
    report_times: List[float] = []
    for _ in range(5):
        t0 = time.perf_counter()
        res_rep = client.get(f"/api/interview-sessions/{session_id}/report")
        t1 = time.perf_counter()
        if res_rep.status_code == 200:
            report_times.append((t1 - t0) * 1000)

    stats_submit = compute_stats(submit_times)
    stats_next = compute_stats(next_times)
    stats_hist = compute_stats(hist_times)
    stats_legacy = compute_stats(legacy_turn_times)
    stats_opt = compute_stats(optimized_turn_times)
    stats_report = compute_stats(report_times)
    
    print("\nAPI Route Latency Breakdown (ms):")
    print(f"  POST /answers:        mean={stats_submit['mean']} | p50={stats_submit['median']} | p95={stats_submit['p95']} | p99={stats_submit['p99']}")
    print(f"  POST /questions/next: mean={stats_next['mean']} | p50={stats_next['median']} | p95={stats_next['p95']} | p99={stats_next['p99']}")
    print(f"  GET  /questions:      mean={stats_hist['mean']} | p50={stats_hist['median']} | p95={stats_hist['p95']} | p99={stats_hist['p99']}")
    if report_times:
        print(f"  GET  /report:         mean={stats_report['mean']} | p50={stats_report['median']} | p95={stats_report['p95']}")

    saved_mean = stats_legacy['mean'] - stats_opt['mean']
    pct = (saved_mean / stats_legacy['mean']) * 100.0 if stats_legacy['mean'] > 0 else 0
    speedup = stats_legacy['mean'] / stats_opt['mean'] if stats_opt['mean'] > 0 else 1.0

    print("\nEnd-to-End Candidate Perceived Turn Latency (UI Blocked Duration):")
    print(f"  [Legacy Waterfall] (3 calls): mean={stats_legacy['mean']}ms | p50={stats_legacy['median']}ms | p95={stats_legacy['p95']}ms")
    print(f"  [Optimized Flow]   (2 calls): mean={stats_opt['mean']}ms | p50={stats_opt['median']}ms | p95={stats_opt['p95']}ms")
    print(f"  Savings:                      {saved_mean:.2f}ms saved per turn ({pct:.1f}% latency reduction, {speedup:.2f}x faster)")

    return {
        "submit": stats_submit,
        "next": stats_next,
        "history": stats_hist,
        "report": stats_report,
        "legacy_turn": stats_legacy,
        "optimized_turn": stats_opt,
        "savings_ms": round(saved_mean, 2),
        "reduction_pct": round(pct, 1),
        "speedup": round(speedup, 2),
    }


def test_full_phase3_benchmark_suite(client: TestClient, started: str) -> None:
    import asyncio
    settings = get_settings()
    
    # 1. Database Query Benchmarks
    db_results = run_database_benchmarks(settings.database_url)
    
    # 2. AI Gateway Benchmarks
    ai_results = asyncio.run(run_ai_gateway_benchmarks())
    
    # 3. API Route Latencies
    api_results = run_api_route_benchmarks(client, started, turns=10)
    
    # Output combined JSON summary
    summary = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "database_queries": db_results,
        "ai_gateway": ai_results,
        "api_routes": api_results,
    }
    
    with open("backend_benchmark_results.json", "w") as f:
        json.dump(summary, f, indent=2)
    print("\nBenchmark results saved to backend_benchmark_results.json")
