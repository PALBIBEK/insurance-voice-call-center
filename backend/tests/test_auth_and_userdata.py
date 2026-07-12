"""Login, seeded demo user, profile tool, and the agent_turn_log projection.

Uses the same full-stack TestClient harness as test_web.py: scripted
FakeChatClient, file-backed sqlite, zero network calls.
"""

import json

from insurance_voice.services.agents.demo_client import DemoChatClient

from tests.fakes import text_response, tool_call_response
from tests.test_web import make_client


def login(client, user_id="Bibek", password="Bibek"):
    return client.post("/api/auth/login", json={"user_id": user_id, "password": password})


def test_login_success_returns_token_and_profile():
    with make_client() as client:
        body = login(client).json()
        assert body["user_id"] == "Bibek"
        assert body["access_token"].count(".") == 1
        policies = body["user_data"]["policies"]
        assert policies[0]["policy_number"] == "POL-1001"


def test_login_rejects_bad_credentials():
    with make_client() as client:
        assert login(client, password="nope").status_code == 401
        assert login(client, user_id="ghost", password="x").status_code == 401


def test_bearer_identity_outranks_body_user_id():
    with make_client() as client:
        token = login(client).json()["access_token"]
        res = client.post(
            "/api/sessions",
            json={"channel": "text", "user_id": "someone-else"},
            headers={"Authorization": f"Bearer {token}"},
        )
        session_id = res.json()["session_id"]
        sessions = client.get("/api/sessions", params={"user_id": "Bibek"}).json()
        assert session_id in {s["session_id"] for s in sessions}

        assert client.post(
            "/api/sessions", json={"channel": "text"}, headers={"Authorization": "Bearer bogus.token"}
        ).status_code == 401


def test_profile_tool_returns_logged_in_user_data():
    script = [
        tool_call_response(("get_caller_profile", {})),
        text_response("You hold POL-1001."),
    ]
    with make_client(script) as client:
        token = login(client).json()["access_token"]
        res = client.post("/api/sessions", json={"channel": "text"},
                          headers={"Authorization": f"Bearer {token}"})
        session_id = res.json()["session_id"]
        client.app.state.ctx.force_agent_for_tests(session_id, "policy")

        with client.websocket_connect(f"/ws/sessions/{session_id}") as ws:
            assert ws.receive_json()["type"] == "connection_ack"
            ws.send_text(json.dumps({"type": "user_message", "data": {"content": "What's my billing status?"}}))
            profile_result = None
            while True:
                event = ws.receive_json()
                if event["type"] == "tool_call_succeeded":
                    assert event["data"]["tool_name"] == "get_caller_profile"
                if event["type"] == "turn_created" and event["data"]["role"] == "agent":
                    break

        # The tool result was persisted into the session history, so the next
        # turn's LLM context contains the profile (the fix for cross-turn
        # policy-number hallucination).
        chat = client.app.state.ctx.runtime.chat_client
        history_roles = [m["role"] for m in chat.requests[-1]["messages"]]
        assert "tool" in history_roles
        tool_msgs = [m for m in chat.requests[-1]["messages"] if m["role"] == "tool"]
        assert any("POL-1001" in m["content"] for m in tool_msgs)


def test_agent_turn_log_reconstructs_session_and_metrics_aggregate():
    script = [
        tool_call_response(("get_billing_status", {"policy_number": "POL-1001"})),
        text_response("All paid."),
    ]
    with make_client(script) as client:
        token = login(client).json()["access_token"]
        res = client.post("/api/sessions", json={"channel": "text"},
                          headers={"Authorization": f"Bearer {token}"})
        session_id = res.json()["session_id"]
        client.app.state.ctx.force_agent_for_tests(session_id, "policy")

        with client.websocket_connect(f"/ws/sessions/{session_id}") as ws:
            assert ws.receive_json()["type"] == "connection_ack"
            ws.send_text(json.dumps({"type": "user_message", "data": {"content": "Billing for POL-1001?"}}))
            while True:
                event = ws.receive_json()
                if event["type"] == "turn_created" and event["data"]["role"] == "agent":
                    break

        log = client.get(f"/api/sessions/{session_id}/agent-log").json()
        steps = [(r["turn_step"], r["step_type"]) for r in log]
        assert steps == [(1, "user_message"), (2, "tool_call"), (3, "agent_message")]
        assert all(r["user_id"] == "Bibek" for r in log)
        tool_row = log[1]
        assert tool_row["tool_name"] == "get_billing_status"
        assert tool_row["tool_args"] == {"policy_number": "POL-1001"}
        assert tool_row["tool_status"] == "succeeded"
        assert tool_row["latency_ms"] is not None

        metrics = client.get("/api/metrics/tools").json()
        by_name = {m["tool_name"]: m for m in metrics}
        assert by_name["get_billing_status"]["attempts"] == 1
        assert by_name["get_billing_status"]["success_rate"] == 1.0
        assert by_name["get_billing_status"]["avg_latency_ms"] is not None


def test_pending_approval_survives_state_loss_and_claim_status_tool_reads_it():
    """The reopened-conversation scenario: claim goes to pending approval,
    the hot session store forgets (= backend restart), the caller asks
    'what about my claim' - the gateway rehydrates from the DB, the agent
    answers via get_claim_status, and no duplicate approval is created."""
    claim = {"policy_number": "POL-1001", "claim_amount": 45000, "documents": ["discharge_summary", "bills", "id_proof"]}
    script = [
        tool_call_response(("request_claim_approval", claim)),
        text_response("Queued for human review."),
        tool_call_response(("get_claim_status", {})),
        text_response("Still pending review."),
    ]
    with make_client(script) as client:
        token = login(client).json()["access_token"]
        res = client.post("/api/sessions", json={"channel": "text"},
                          headers={"Authorization": f"Bearer {token}"})
        session_id = res.json()["session_id"]
        ctx = client.app.state.ctx
        ctx.force_agent_for_tests(session_id, "claims")

        def turn(text):
            with client.websocket_connect(f"/ws/sessions/{session_id}") as ws:
                assert ws.receive_json()["type"] == "connection_ack"
                ws.send_text(json.dumps({"type": "user_message", "data": {"content": text}}))
                while True:
                    event = ws.receive_json()
                    if event["type"] == "turn_created" and event["data"]["role"] == "agent":
                        return event["data"]["content"]

        turn("Please submit my claim")
        pending = client.get("/api/approvals").json()
        assert len(pending) == 1
        assert pending[0]["requested_by"] == "Bibek Pal"  # reviewer sees who is asking
        assert pending[0]["claim_draft"]["policy_number"] == "POL-1001"

        # Simulate a backend restart: the hot store forgets everything.
        ctx.store._state.clear()
        ctx.store._history.clear()
        ctx.force_agent_for_tests(session_id, "claims")

        reply = turn("What about my claim?")
        assert "pending" in reply.lower()
        # The tool saw the pending claim from the durable rows
        chat = ctx.runtime.chat_client
        tool_msgs = [m for m in chat.requests[-1]["messages"] if m["role"] == "tool"]
        assert any('"status": "pending"' in m["content"] for m in tool_msgs)
        # Still exactly one pending approval - nothing was re-filed
        assert len(client.get("/api/approvals").json()) == 1


def test_request_approval_is_idempotent_per_session():
    claim = {"policy_number": "POL-1001", "claim_amount": 45000, "documents": ["bills"]}
    script = [
        tool_call_response(("request_claim_approval", claim)),
        text_response("Queued."),
        tool_call_response(("request_claim_approval", claim)),
        text_response("Already queued."),
    ]
    with make_client(script) as client:
        res = client.post("/api/sessions", json={"channel": "text"})
        session_id = res.json()["session_id"]
        client.app.state.ctx.force_agent_for_tests(session_id, "claims")

        with client.websocket_connect(f"/ws/sessions/{session_id}") as ws:
            assert ws.receive_json()["type"] == "connection_ack"
            for text in ("submit my claim", "submit it again please"):
                ws.send_text(json.dumps({"type": "user_message", "data": {"content": text}}))
                while True:
                    event = ws.receive_json()
                    if event["type"] == "turn_created" and event["data"]["role"] == "agent":
                        break

        pending = client.get("/api/approvals").json()
        assert len(pending) == 1


# ---- demo-brain profile behavior (pure function tests, no stack) ----------


def _tools(*names):
    return [{"type": "function", "function": {"name": n, "description": "", "parameters": {}}} for n in names]


async def _run_demo(messages, tools):
    return await DemoChatClient()._create(model="demo", messages=messages, tools=tools)


def test_demo_policy_agent_fetches_profile_before_asking(anyio_backend="asyncio"):
    import asyncio

    messages = [
        {"role": "system", "content": "policy and billing specialist"},
        {"role": "user", "content": "What's my billing status?"},
    ]
    tools = _tools("get_caller_profile", "get_billing_status", "handoff_to_claims")
    response = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(_run_demo(messages, tools))
    call = response.choices[0].message.tool_calls[0]
    assert call.function.name == "get_caller_profile"


def test_demo_policy_agent_uses_profile_policy_number():
    import asyncio

    profile = {"found": True, "profile": {"name": "Bibek Pal", "city": "Mumbai",
                                          "policies": [{"policy_number": "POL-1001", "plan": "Gold"}]}}
    messages = [
        {"role": "system", "content": "policy and billing specialist"},
        {"role": "user", "content": "What's my billing status?"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
            "function": {"name": "get_caller_profile", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps(profile)},
    ]
    tools = _tools("get_caller_profile", "get_billing_status", "handoff_to_claims")
    response = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(_run_demo(messages, tools))
    call = response.choices[0].message.tool_calls[0]
    assert call.function.name == "get_billing_status"
    assert json.loads(call.function.arguments) == {"policy_number": "POL-1001"}
