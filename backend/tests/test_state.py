"""Session store (hot state) + event bus (pub/sub fan-out).

Tests run against the in-memory backend; the Redis backend implements the
same interface and is exercised in docker-compose.
"""

import asyncio

from insurance_voice.services.event_bus import InMemoryEventBus
from insurance_voice.services.session_store import InMemorySessionStore


async def test_session_state_roundtrip():
    store = InMemorySessionStore()
    await store.set_state("s1", {"status": "active", "current_agent": "triage", "stagnant_turn_count": 0})
    state = await store.get_state("s1")
    assert state["current_agent"] == "triage"

    await store.update_state("s1", current_agent="claims", status="pending_approval")
    state = await store.get_state("s1")
    assert state["current_agent"] == "claims"
    assert state["status"] == "pending_approval"
    assert state["stagnant_turn_count"] == 0  # untouched keys survive updates


async def test_sessions_are_isolated():
    store = InMemorySessionStore()
    await store.set_state("s1", {"current_agent": "claims"})
    await store.set_state("s2", {"current_agent": "policy"})
    assert (await store.get_state("s1"))["current_agent"] == "claims"
    assert (await store.get_state("s2"))["current_agent"] == "policy"


async def test_history_window_is_bounded():
    store = InMemorySessionStore(history_limit=3)
    for i in range(5):
        await store.append_history("s1", {"role": "user", "content": f"m{i}"})
    history = await store.get_history("s1")
    assert [h["content"] for h in history] == ["m2", "m3", "m4"]


async def test_event_bus_delivers_to_session_subscribers_only():
    bus = InMemoryEventBus()
    received_s1: list[dict] = []
    received_s2: list[dict] = []

    async with bus.subscribe("s1") as q1, bus.subscribe("s2") as q2:
        await bus.publish("s1", {"type": "turn_created", "data": {"content": "hi"}})
        received_s1.append(await asyncio.wait_for(q1.get(), timeout=1))
        assert q2.empty()
        await bus.publish("s2", {"type": "agent_handoff", "data": {}})
        received_s2.append(await asyncio.wait_for(q2.get(), timeout=1))

    assert received_s1[0]["type"] == "turn_created"
    assert received_s2[0]["type"] == "agent_handoff"


async def test_event_bus_fan_out_to_multiple_subscribers():
    bus = InMemoryEventBus()
    async with bus.subscribe("s1") as q1, bus.subscribe("s1") as q2:
        await bus.publish("s1", {"type": "approval_required", "data": {"approval_id": 7}})
        e1 = await asyncio.wait_for(q1.get(), timeout=1)
        e2 = await asyncio.wait_for(q2.get(), timeout=1)
    assert e1 == e2


async def test_publish_with_no_subscribers_does_not_error():
    bus = InMemoryEventBus()
    await bus.publish("ghost", {"type": "session_ended", "data": {}})


def test_backend_selection_from_settings():
    from insurance_voice.services.event_bus import build_event_bus
    from insurance_voice.services.session_store import build_session_store

    assert isinstance(build_session_store(""), InMemorySessionStore)
    assert isinstance(build_event_bus(""), InMemoryEventBus)
    # redis:// URLs select the Redis implementations (not connected here)
    from insurance_voice.services.event_bus import RedisEventBus
    from insurance_voice.services.session_store import RedisSessionStore

    assert isinstance(build_session_store("redis://localhost:6379/0"), RedisSessionStore)
    assert isinstance(build_event_bus("redis://localhost:6379/0"), RedisEventBus)
