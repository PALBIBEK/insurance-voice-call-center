"""Tools layer: latency injection, failure injection, retry/backoff, escalation.

Latency is injected via asyncio.sleep so these tests shrink the window to
milliseconds through ToolPolicy rather than monkeypatching sleep - the code
path exercised is exactly the production one.
"""

import asyncio
import time

import pytest

from insurance_voice.services.tools.base import (
    ToolExhaustedError,
    ToolPolicy,
    ToolRegistry,
    UnknownToolError,
)


def fast_policy(**overrides) -> ToolPolicy:
    defaults = dict(
        latency_min_s=0.01,
        latency_max_s=0.02,
        failure_rate=0.0,
        max_retries=3,
        retry_backoff_base_s=0.01,
    )
    defaults.update(overrides)
    return ToolPolicy(**defaults)


def make_registry(policy: ToolPolicy) -> ToolRegistry:
    registry = ToolRegistry(policy=policy)

    @registry.tool("echo", description="echo back", parameters={"type": "object", "properties": {"x": {"type": "string"}}})
    async def echo(x: str) -> dict:
        return {"echo": x}

    return registry


async def test_tool_call_injects_latency():
    registry = make_registry(fast_policy(latency_min_s=0.05, latency_max_s=0.06))
    start = time.perf_counter()
    result = await registry.invoke("echo", {"x": "hi"}, session_id="s1")
    elapsed = time.perf_counter() - start
    assert result.output == {"echo": "hi"}
    assert elapsed >= 0.05
    assert result.attempts == 1
    assert result.latency_ms >= 50


async def test_forced_failure_then_retry_succeeds():
    registry = make_registry(fast_policy())
    # Force exactly the first 2 attempts to fail -> succeeds on attempt 3
    registry.force_failures("echo", count=2)
    result = await registry.invoke("echo", {"x": "hi"}, session_id="s1")
    assert result.output == {"echo": "hi"}
    assert result.attempts == 3


async def test_retry_ceiling_raises_tool_exhausted():
    registry = make_registry(fast_policy(max_retries=2))
    registry.force_failures("echo", count=99)
    with pytest.raises(ToolExhaustedError) as exc:
        await registry.invoke("echo", {"x": "hi"}, session_id="s1")
    assert exc.value.tool_name == "echo"
    assert exc.value.attempts == 2


async def test_unknown_tool_is_flagged_not_executed():
    registry = make_registry(fast_policy())
    with pytest.raises(UnknownToolError):
        await registry.invoke("not_a_tool", {}, session_id="s1")


async def test_concurrent_invocations_interleave_not_serialize():
    """10 concurrent calls with ~50ms latency each must finish in ~one
    latency window, not ten - proves nothing blocks the event loop."""
    registry = make_registry(fast_policy(latency_min_s=0.05, latency_max_s=0.05))
    start = time.perf_counter()
    results = await asyncio.gather(
        *(registry.invoke("echo", {"x": str(i)}, session_id=f"s{i}") for i in range(10))
    )
    elapsed = time.perf_counter() - start
    assert len(results) == 10
    assert elapsed < 0.3  # serialized would be >= 0.5s


async def test_observer_sees_lifecycle_events():
    events: list[tuple[str, dict]] = []

    async def observer(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    registry = make_registry(fast_policy())
    registry.force_failures("echo", count=1)
    await registry.invoke("echo", {"x": "hi"}, session_id="s1", observer=observer)

    types = [e[0] for e in events]
    # attempt 1 starts, fails with will_retry, attempt 2 starts, succeeds
    assert types == ["tool_call_started", "tool_call_failed", "tool_call_started", "tool_call_succeeded"]
    assert events[1][1]["will_retry"] is True
    assert events[0][1]["tool_name"] == "echo"


async def test_insurance_tools_registered_with_schemas():
    from insurance_voice.services.tools import build_default_registry

    registry = build_default_registry(fast_policy())
    names = set(registry.tool_names)
    assert {
        "get_policy_details",
        "get_hospital_network",
        "get_billing_status",
        "calculate_claim_probability",
        "submit_claim",
    } <= names
    # Each tool exposes an OpenAI-compatible function spec for the LLM
    specs = registry.openai_tool_specs()
    assert all(s["type"] == "function" for s in specs)
    assert {s["function"]["name"] for s in specs} == names


async def test_claim_probability_is_deterministic_for_same_input():
    from insurance_voice.services.tools import build_default_registry

    registry = build_default_registry(fast_policy())
    args = {"policy_number": "POL-1001", "claim_amount": 50000, "documents": ["discharge_summary", "bills"]}
    r1 = await registry.invoke("calculate_claim_probability", args, session_id="s1")
    r2 = await registry.invoke("calculate_claim_probability", args, session_id="s1")
    assert r1.output == r2.output
    assert 0.0 <= r1.output["probability"] <= 1.0
