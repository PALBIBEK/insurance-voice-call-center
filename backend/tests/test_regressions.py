"""Regression locks for the three concurrency/ordering bugs found during
the web-layer build.

1. In-memory SQLite + concurrent tasks: every pooled connection to
   :memory: is a different database; the StaticPool workaround shares one
   connection, but concurrent sessions interleaving on one connection
   corrupt each other. Database must make concurrent use safe.
2. A WS disconnect must never abort an in-flight turn: business logic and
   transcript persistence complete even if the browser goes away.
3. Persist-then-publish: at the moment a turn_created event is observable,
   the corresponding rows must already be committed and readable.
"""

import asyncio
import tempfile
import time
import uuid

import sqlalchemy as sa

from insurance_voice.db.models import CallSession, Turn
from insurance_voice.db.session import Database


# ---------------------------------------------------------------- bug 1


async def test_inmemory_db_survives_concurrent_sessions():
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.create_all()

    async def writer(i: int) -> None:
        session_id = str(uuid.uuid4())
        async with db.session() as s:
            s.add(CallSession(id=session_id, channel="text"))
            await s.commit()
        async with db.session() as s:
            s.add(Turn(session_id=session_id, role="user", content=f"msg {i}"))
            await s.commit()
        async with db.session() as s:
            rows = (await s.scalars(sa.select(Turn).where(Turn.session_id == session_id))).all()
            assert len(rows) == 1

    # 25 interleaving tasks, several sessions alive at once
    await asyncio.gather(*(writer(i) for i in range(25)))

    async with db.session() as s:
        count = await s.scalar(sa.select(sa.func.count()).select_from(Turn))
        assert count == 25
    await db.dispose()


# ---------------------------------------------------------------- bug 2


def test_ws_disconnect_does_not_abort_inflight_turn():
    from tests.test_web import create_session, make_client
    from tests.fakes import text_response, tool_call_response

    script = [
        tool_call_response(("get_billing_status", {"policy_number": "POL-2002"})),
        text_response("You owe Rs 8450."),
    ]
    with make_client(script) as client:
        session_id = create_session(client)
        client.app.state.ctx.force_agent_for_tests(session_id, "policy")
        with client.websocket_connect(f"/ws/sessions/{session_id}") as ws:
            ws.receive_json()  # ack
            ws.send_json({"type": "user_message", "data": {"content": "billing status please"}})
            # receive only the user's echo, then vanish mid-tool-call
            event = ws.receive_json()
            assert event["type"] == "turn_created" and event["data"]["role"] == "user"

        # the turn must still complete and persist after the disconnect
        deadline = time.time() + 5
        while time.time() < deadline:
            turns = client.get(f"/api/sessions/{session_id}/turns").json()
            if len(turns) == 2:
                break
            time.sleep(0.05)
        assert [t["role"] for t in turns] == ["user", "agent"]
        assert "8450" in turns[1]["content"]


# ---------------------------------------------------------------- bug 3


async def test_turn_event_published_only_after_row_committed():
    from insurance_voice.services.agents.runtime import AgentRuntime
    from insurance_voice.services.drift_service import DriftService
    from insurance_voice.services.event_bus import InMemoryEventBus
    from insurance_voice.services.gateway import GatewayService
    from insurance_voice.services.session_store import InMemorySessionStore
    from insurance_voice.services.tools import ToolPolicy, build_default_registry
    from tests.fakes import FakeChatClient, text_response

    db = Database(f"sqlite+aiosqlite:///{tempfile.gettempdir()}/ivcc-reg-{uuid.uuid4().hex}.db")
    await db.create_all()
    store = InMemorySessionStore()
    bus = InMemoryEventBus()
    policy = ToolPolicy(latency_min_s=0.01, latency_max_s=0.02, failure_rate=0.0, max_retries=2,
                        retry_backoff_base_s=0.01)
    runtime = AgentRuntime(
        chat_client=FakeChatClient([text_response("Hello there.")]),
        registry=build_default_registry(policy),
        store=store, bus=bus, model_triage="m", model_specialist="m",
    )
    drift = DriftService(store=store, bus=bus, max_offdomain_turns=2, max_stagnant_turns=3)
    gateway = GatewayService(db=db, store=store, bus=bus, runtime=runtime, drift=drift)

    session_id = str(uuid.uuid4())
    async with db.session() as s:
        s.add(CallSession(id=session_id, channel="text"))
        await s.commit()
    await store.set_state(session_id, {"status": "active", "current_agent": "triage", "stagnant_turn_count": 0})

    rows_at_event: dict[str, int] = {}

    async def observe() -> None:
        async with bus.subscribe(session_id) as queue:
            while True:
                event = await queue.get()
                if event["type"] == "turn_created":
                    # at the instant the event is observable, count committed rows
                    async with db.session() as s:
                        count = await s.scalar(
                            sa.select(sa.func.count()).select_from(Turn).where(Turn.session_id == session_id)
                        )
                    rows_at_event[event["data"]["role"]] = count
                    if event["data"]["role"] == "agent":
                        return

    observer_task = asyncio.create_task(observe())
    await asyncio.sleep(0.05)  # let the subscriber attach
    await gateway.handle_turn(session_id, "hello, insurance question")
    await asyncio.wait_for(observer_task, timeout=5)

    assert rows_at_event["user"] >= 1   # user row committed before its event
    assert rows_at_event["agent"] == 2  # both rows committed before the agent event
    await db.dispose()
