"""Readable agent-log viewer - the step-by-step reconstruction of every
conversation (messages, tool calls, handoffs, guardrails, HITL, submissions).

Talks to the running service's observability API, so it works identically
against docker compose and local dev. Stdlib only - no install needed.

    python scripts/agent_logs.py                        # every session of the demo user (+ anonymous voice sessions)
    python scripts/agent_logs.py <session_id...>        # only these (prefix ok)
    python scripts/agent_logs.py --user Bibek --base-url http://localhost:8000
"""
import argparse
import json
import sys
import urllib.error
import urllib.request


def fetch(base_url: str, path: str, api_key: str) -> object:
    request = urllib.request.Request(base_url + path)
    if api_key:
        request.add_header("X-API-Key", api_key)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def describe(row: dict) -> str:
    st = row["step_type"]
    if st in ("user_message", "agent_message"):
        who = "user" if st == "user_message" else f"agent:{row['agent_name']}"
        return f"{who:<14} {row['message']}"
    if st == "tool_call":
        args = json.dumps(row["tool_args"]) if row.get("tool_args") else ""
        status = row["tool_status"]
        lat = f"{row['latency_ms']}ms" if row.get("latency_ms") is not None else "?"
        extra = f" err={row['message']}" if status != "succeeded" and row.get("message") else ""
        return f"tool           {row['tool_name']}({args}) [{status}, {lat}]{extra}"
    if st == "handoff":
        return f"handoff        {row['message']}"
    return f"{st:<14} {row.get('message') or ''}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prefixes", nargs="*", help="session id prefixes to show (default: all)")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--user", default="Bibek", help="whose sessions to list (anonymous voice sessions are always included)")
    parser.add_argument("--api-key", default="", help="X-API-Key when the guard is enabled")
    args = parser.parse_args()

    try:
        sessions: list[dict] = []
        for user_id in dict.fromkeys([args.user, "anonymous"]):  # ordered, deduped
            sessions += fetch(args.base_url, f"/api/sessions?user_id={user_id}&limit=200", args.api_key)
    except (urllib.error.URLError, OSError) as err:
        print(f"Cannot reach the service at {args.base_url} ({err}).\n"
              "Start it first: docker compose up -d  (or python -m insurance_voice.asgi)")
        return 1

    sessions.reverse()  # oldest first reads like a story
    if args.prefixes:
        sessions = [s for s in sessions if any(s["session_id"].startswith(p) for p in args.prefixes)]
    if not sessions:
        print("No matching sessions.")
        return 0

    for session in sessions:
        rows = fetch(args.base_url, f"/api/sessions/{session['session_id']}/agent-log", args.api_key)
        if not rows:
            continue
        print(f"\n=== session {session['session_id'][:8]} | user={rows[0].get('user_id')} "
              f"| status={session['status']} | agent={session.get('current_agent')} ===")
        for row in rows:
            print(f"  {row['turn_step']:>2}  {describe(row)}")

    print("\n=== tool metrics (all sessions) ===")
    for m in fetch(args.base_url, "/api/metrics/tools", args.api_key):
        lat = f"{float(m['avg_latency_ms']):.0f}ms" if m.get("avg_latency_ms") is not None else "n/a"
        print(f"  {m['tool_name']:<28} attempts={m['attempts']:<4} "
              f"success_rate={m['success_rate']:.0%}  avg_latency={lat}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
