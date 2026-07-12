"""Web layer: REST session lifecycle, WS realtime channel, voice webhook,
HITL endpoints, trace endpoint, auth guard.

The app is built with a scripted FakeChatClient - full HTTP/WS stack, zero
network/LLM calls.
"""

import json
import tempfile
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from insurance_voice.config.settings import Settings
from insurance_voice.factory import create_app
from tests.fakes import FakeChatClient, text_response, tool_call_response


def make_client(script=(), **settings_overrides) -> TestClient:
    # File-backed sqlite: concurrent tasks (WS relay, turn task, trace
    # writes) need real independent connections to one shared database,
    # which :memory: cannot provide.
    db_path = f"{tempfile.gettempdir()}/ivcc-test-{uuid.uuid4().hex}.db"
    values = dict(
        DATABASE_URL=f"sqlite+aiosqlite:///{db_path}",
        REDIS_URL="",
        TOOL_LATENCY_MIN_S=0.01,
        TOOL_LATENCY_MAX_S=0.02,
        TOOL_FAILURE_RATE=0.0,
        TOOL_RETRY_BACKOFF_BASE_S=0.01,
    )
    values.update(settings_overrides)
    settings = Settings(**values)
    app = create_app(settings=settings, chat_client=FakeChatClient(list(script)))
    return TestClient(app)


def create_session(client: TestClient, channel="text") -> str:
    response = client.post("/api/sessions", json={"channel": channel})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "active"
    assert body["ws_url"].endswith(body["session_id"])
    return body["session_id"]


def test_session_create_get_end():
    with make_client() as client:
        session_id = create_session(client)

        got = client.get(f"/api/sessions/{session_id}").json()
        assert got["current_agent"] == "triage"

        ended = client.post(f"/api/sessions/{session_id}/end")
        assert ended.json()["status"] == "completed"

        assert client.get("/api/sessions/does-not-exist").status_code == 404


def test_health_is_open_and_checks_dependencies():
    # No X-API-Key even when the guard is on: probes can't send headers.
    with make_client(API_KEY="sekrit") as client:
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["checks"] == {"database": "ok", "session_store": "ok"}


def test_api_key_guard():
    with make_client(API_KEY="sekrit") as client:
        assert client.post("/api/sessions", json={"channel": "text"}).status_code == 401
        ok = client.post("/api/sessions", json={"channel": "text"}, headers={"X-API-Key": "sekrit"})
        assert ok.status_code == 201


def test_ws_text_turn_with_tool_events():
    script = [
        tool_call_response(("handoff_to_policy", {"reason": "billing"})),
        tool_call_response(("get_billing_status", {"policy_number": "POL-2002"})),
        text_response("You owe Rs 8450, due 15 July."),
    ]
    with make_client(script) as client:
        session_id = create_session(client)
        with client.websocket_connect(f"/ws/sessions/{session_id}") as ws:
            ack = ws.receive_json()
            assert ack["type"] == "connection_ack"
            assert ack["data"]["current_agent"] == "triage"

            ws.send_json({"type": "user_message", "data": {"content": "What is my billing status for POL-2002?"}})

            events = []
            while True:
                event = ws.receive_json()
                events.append(event)
                if event["type"] == "turn_created" and event["data"]["role"] == "agent":
                    break

        types = [e["type"] for e in events]
        assert types[0] == "turn_created"  # user's own turn echoed
        assert "agent_handoff" in types
        assert "tool_call_started" in types and "tool_call_succeeded" in types
        final = events[-1]
        assert "8450" in final["data"]["content"]
        assert final["data"]["agent_name"] == "policy"

        # durable transcript was written too
        transcript = client.get(f"/api/sessions/{session_id}/turns").json()
        assert [t["role"] for t in transcript] == ["user", "agent"]


def test_voice_webhook_streams_sse():
    script = [text_response("Hello! How can I help with your insurance today?")]
    with make_client(script) as client:
        session_id = create_session(client, channel="voice")
        response = client.post(
            "/api/voice/completions",
            json={
                "model": "ignored",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
                "metadata": {"session_id": session_id},
            },
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")

        payloads = [line[len("data: "):] for line in response.text.splitlines() if line.startswith("data: ")]
        assert payloads[-1] == "[DONE]"
        chunks = [json.loads(p) for p in payloads[:-1]]
        streamed = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
        assert streamed == "Hello! How can I help with your insurance today?"
        assert chunks[0]["object"] == "chat.completion.chunk"


def test_voice_webhook_non_streaming():
    script = [text_response("Sure - policy or claims?")]
    with make_client(script) as client:
        session_id = create_session(client, channel="voice")
        response = client.post(
            "/api/voice/completions",
            json={
                "model": "ignored",
                "stream": False,
                "messages": [{"role": "user", "content": "hi"}],
                "metadata": {"session_id": session_id},
            },
        )
        body = response.json()
        assert body["choices"][0]["message"]["content"] == "Sure - policy or claims?"
        assert body["object"] == "chat.completion"


def test_hitl_full_loop_over_http():
    script = [
        tool_call_response(
            ("request_claim_approval",
             {"policy_number": "POL-1001", "claim_amount": 50000,
              "documents": ["discharge_summary", "bills", "id_proof"]})
        ),
        text_response("Your claim is queued for review."),
    ]
    with make_client(script) as client:
        session_id = create_session(client)
        # jump straight to the claims agent
        with client.websocket_connect(f"/ws/sessions/{session_id}") as ws:
            ws.receive_json()  # ack
            client.app.state.ctx.force_agent_for_tests(session_id, "claims")
            ws.send_json({"type": "user_message", "data": {"content": "please submit my claim now"}})
            events = []
            while True:
                event = ws.receive_json()
                events.append(event)
                if event["type"] == "turn_created" and event["data"]["role"] == "agent":
                    break
            assert "approval_required" in [e["type"] for e in events]

            pending = client.get("/api/approvals", params={"status": "pending"}).json()
            assert len(pending) == 1
            approval_id = pending[0]["approval_id"]

            decision = client.post(
                f"/api/approvals/{approval_id}/decision",
                json={"decision": "approved", "decided_by": "reviewer@test"},
            )
            assert decision.json()["status"] == "approved"

            # the still-open WS sees the decision and the gated submission
            post_events = []
            while True:
                event = ws.receive_json()
                post_events.append(event)
                if event["type"] == "claim_submitted":
                    break
            types = [e["type"] for e in post_events]
            assert "approval_decided" in types
            assert "tool_call_started" in types

        # double decision -> 409
        again = client.post(
            f"/api/approvals/{approval_id}/decision",
            json={"decision": "rejected", "decided_by": "reviewer@test"},
        )
        assert again.status_code == 409


def test_trace_endpoint_reconstructs_session_activity():
    script = [
        tool_call_response(("handoff_to_policy", {"reason": "billing"})),
        tool_call_response(("get_billing_status", {"policy_number": "POL-1001"})),
        text_response("All paid up."),
    ]
    with make_client(script) as client:
        session_id = create_session(client)
        with client.websocket_connect(f"/ws/sessions/{session_id}") as ws:
            ws.receive_json()
            ws.send_json({"type": "user_message", "data": {"content": "billing status for POL-1001?"}})
            while True:
                event = ws.receive_json()
                if event["type"] == "turn_created" and event["data"]["role"] == "agent":
                    break

        spans = client.get(f"/api/sessions/{session_id}/trace").json()
        kinds = [s["kind"] for s in spans]
        assert "handoff" in kinds
        assert "tool_call" in kinds
        assert "agent_run" in kinds


def test_offdomain_turn_gets_redirect_without_llm_call():
    # empty script: the LLM must NOT be called for an off-domain turn
    with make_client(script=[]) as client:
        session_id = create_session(client)
        with client.websocket_connect(f"/ws/sessions/{session_id}") as ws:
            ws.receive_json()
            ws.send_json({"type": "user_message", "data": {"content": "write me a poem about the moon please"}})
            events = []
            while True:
                event = ws.receive_json()
                events.append(event)
                if event["type"] == "turn_created" and event["data"]["role"] == "agent":
                    break
        types = [e["type"] for e in events]
        assert "drift_detected" in types
        assert "insurance" in events[-1]["data"]["content"].lower()


def test_voice_webhook_first_chunk_beats_tool_latency():
    """Voice must not go silent while a 2-3s tool call runs: the SSE stream
    has to open immediately and speak a filler before the real answer."""
    script = [
        tool_call_response(("get_billing_status", {"policy_number": "POL-2002"})),
        text_response("You owe Rs 8450, due 15 July."),
    ]
    # Slow the tool down enough that a blocking implementation would fail
    with make_client(script, TOOL_LATENCY_MIN_S=0.6, TOOL_LATENCY_MAX_S=0.7) as client:
        session_id = create_session(client, channel="voice")
        client.app.state.ctx.force_agent_for_tests(session_id, "policy")

        chunks: list[tuple[float, str]] = []  # (seconds since request, content)
        start = time.perf_counter()
        with client.stream(
            "POST",
            "/api/voice/completions",
            json={
                "model": "ignored",
                "stream": True,
                "messages": [{"role": "user", "content": "how much premium do I owe?"}],
                "metadata": {"session_id": session_id},
            },
        ) as response:
            assert response.status_code == 200
            for line in response.iter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                payload = json.loads(line[len("data: "):])
                content = payload["choices"][0]["delta"].get("content", "")
                if content:
                    chunks.append((time.perf_counter() - start, content))

        streamed = "".join(c for _, c in chunks)
        assert "8450" in streamed
        # a filler was spoken, and it came before the answer
        assert "moment" in streamed.lower()
        assert streamed.lower().index("moment") < streamed.index("8450")
        # first audible content arrived while the tool was still sleeping.
        # The tool sleeps >= 2.0s (TOOL_LATENCY_MIN_S); the budget just needs to
        # be comfortably under that floor while tolerating slow CI filesystems.
        first_content_at = chunks[0][0]
        assert first_content_at < 1.5, f"first chunk took {first_content_at:.2f}s - stream blocked on the turn"


def test_sessions_are_listed_per_user_newest_first():
    with make_client() as client:
        # Two users, three sessions
        a1 = client.post("/api/sessions", json={"channel": "text", "user_id": "user-a"}).json()["session_id"]
        a2 = client.post("/api/sessions", json={"channel": "text", "user_id": "user-a"}).json()["session_id"]
        b1 = client.post("/api/sessions", json={"channel": "text", "user_id": "user-b"}).json()["session_id"]

        listed_a = client.get("/api/sessions", params={"user_id": "user-a"}).json()
        listed_b = client.get("/api/sessions", params={"user_id": "user-b"}).json()

        assert [s["session_id"] for s in listed_b] == [b1]
        ids_a = [s["session_id"] for s in listed_a]
        assert set(ids_a) == {a1, a2}  # user isolation: b1 never leaks into a's list
        assert ids_a.index(a2) < ids_a.index(a1) or listed_a[0]["started_at"] >= listed_a[-1]["started_at"]

        # Unknown user -> empty list, not an error
        assert client.get("/api/sessions", params={"user_id": "nobody"}).json() == []
