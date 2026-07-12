"""Concurrency load test.

Fires N concurrent sessions at the running service; each session performs
a triage->policy handoff turn that includes a real 2-3s injected tool
latency. If the event loop or DB pool serialized anything, total wall time
would approach N * ~3s; the pass criterion is that it stays within a few
multiples of a single turn.

Usage:
    python scripts/load_test.py --base-url http://localhost:8321 --sessions 30
"""

import argparse
import asyncio
import statistics
import sys
import time

import httpx


PROMPT = "How much premium do I owe? My policy is POL-2002"


async def one_session(client: httpx.AsyncClient, results: list[dict]) -> None:
    start = time.perf_counter()
    outcome = {"ok": False, "latency_s": 0.0, "error": ""}
    try:
        created = await client.post("/api/sessions", json={"channel": "text"})
        created.raise_for_status()
        session_id = created.json()["session_id"]

        response = await client.post(
            "/api/voice/completions",
            json={
                "model": "load-test",
                "stream": False,
                "messages": [{"role": "user", "content": PROMPT}],
                "metadata": {"session_id": session_id},
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        outcome["ok"] = "8450" in content or "due" in content.lower()
        if not outcome["ok"]:
            outcome["error"] = f"unexpected reply: {content[:80]}"
    except Exception as exc:  # noqa: BLE001 - report, don't crash the run
        outcome["error"] = f"{type(exc).__name__}: {exc}"
    outcome["latency_s"] = time.perf_counter() - start
    results.append(outcome)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8321")
    parser.add_argument("--sessions", type=int, default=30)
    parser.add_argument("--waves", type=int, default=2, help="sequential waves of concurrent sessions")
    args = parser.parse_args()

    limits = httpx.Limits(max_connections=args.sessions + 10)
    print(f"Load test: {args.waves} wave(s) x {args.sessions} concurrent sessions -> {args.base_url}")
    print(f"Each turn includes one mock tool call with 2-3s injected latency.\n")

    all_results: list[dict] = []
    async with httpx.AsyncClient(base_url=args.base_url, timeout=60, limits=limits) as client:
        for wave in range(1, args.waves + 1):
            results: list[dict] = []
            wave_start = time.perf_counter()
            await asyncio.gather(*(one_session(client, results) for _ in range(args.sessions)))
            wave_wall = time.perf_counter() - wave_start

            latencies = sorted(r["latency_s"] for r in results)
            ok = sum(r["ok"] for r in results)
            print(f"wave {wave}: {ok}/{len(results)} ok | wall {wave_wall:.2f}s | "
                  f"p50 {statistics.median(latencies):.2f}s | "
                  f"p95 {latencies[int(len(latencies) * 0.95) - 1]:.2f}s | "
                  f"max {latencies[-1]:.2f}s")
            for r in results:
                if r["error"]:
                    print("   ERROR:", r["error"])
            all_results.extend(results)

    total_ok = sum(r["ok"] for r in all_results)
    total = len(all_results)
    latencies = sorted(r["latency_s"] for r in all_results)
    serialized_estimate = total * 2.5

    print(f"\n== SUMMARY ==")
    print(f"sessions: {total}, succeeded: {total_ok}, failed: {total - total_ok}")
    print(f"latency p50={statistics.median(latencies):.2f}s p95={latencies[int(len(latencies)*0.95)-1]:.2f}s "
          f"max={latencies[-1]:.2f}s")
    print(f"(fully serialized execution would have taken ~{serialized_estimate:.0f}s of tool latency alone)")

    if total_ok == total and latencies[-1] < 15:
        print("RESULT: PASS - concurrent async tool calls with fixed latency, no blocking, no failures")
        return 0
    print("RESULT: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
