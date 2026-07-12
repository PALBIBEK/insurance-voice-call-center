"""Agent runtime: intent -> handoff -> tool call -> response, deterministically.

The LLM is a scripted fake; the tool layer runs for real (millisecond
latency policy), so these tests exercise the actual orchestration loop.
"""

import pytest

from insurance_voice.services.agents.runtime import AgentRuntime
from insurance_voice.services.event_bus import InMemoryEventBus
from insurance_voice.services.session_store import InMemorySessionStore
from insurance_voice.services.tools import ToolPolicy, build_default_registry
from tests.fakes import FakeChatClient, text_response, tool_call_response


def make_runtime(script, **kwargs):
    policy = ToolPolicy(
        latency_min_s=0.01, latency_max_s=0.02, failure_rate=0.0, max_retries=2, retry_backoff_base_s=0.01
    )
    bus = InMemoryEventBus()
    store = InMemorySessionStore()
    runtime = AgentRuntime(
        chat_client=FakeChatClient(script),
        registry=build_default_registry(policy),
        store=store,
        bus=bus,
        model_triage="fake-cheap",
        model_specialist="fake-strong",
        **kwargs,
    )
    return runtime, store, bus


class EventRecorder:
    def __init__(self, bus: InMemoryEventBus, session_id: str):
        self.bus = bus
        self.session_id = session_id
        self.events: list[dict] = []

    async def __aenter__(self):
        self._cm = self.bus.subscribe(self.session_id)
        self.queue = await self._cm.__aenter__()
        return self

    async def __aexit__(self, *exc):
        while not self.queue.empty():
            self.events.append(self.queue.get_nowait())
        await self._cm.__aexit__(*exc)

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]


async def test_triage_hands_off_to_policy_then_policy_answers():
    script = [
        tool_call_response(("handoff_to_policy", {"reason": "billing question"})),  # triage turn
        text_response("Your premium is fully paid."),  # policy agent takes over
    ]
    runtime, store, bus = make_runtime(script)
    await store.set_state("s1", {"status": "active", "current_agent": "triage", "stagnant_turn_count": 0})

    async with EventRecorder(bus, "s1") as rec:
        result = await runtime.run_turn("s1", "What's my billing status?")

    assert result.text == "Your premium is fully paid."
    assert result.agent_name == "policy"
    assert (await store.get_state("s1"))["current_agent"] == "policy"
    assert "agent_handoff" in rec.types()
    handoff = next(e for e in rec.events if e["type"] == "agent_handoff")
    assert handoff["data"]["from_agent"] == "triage"
    assert handoff["data"]["to_agent"] == "policy"
    # transcript (turn_created) events are the gateway's responsibility,
    # published only after the rows are committed - not the runtime's
    assert "turn_created" not in rec.types()


async def test_specialist_tool_call_roundtrip():
    script = [
        tool_call_response(("get_billing_status", {"policy_number": "POL-2002"})),
        text_response("You have Rs 8450 due on 15 July."),
    ]
    runtime, store, bus = make_runtime(script)
    await store.set_state("s1", {"status": "active", "current_agent": "policy", "stagnant_turn_count": 0})

    async with EventRecorder(bus, "s1") as rec:
        result = await runtime.run_turn("s1", "How much premium do I owe?")

    assert "8450" in result.text
    types = rec.types()
    assert "tool_call_started" in types and "tool_call_succeeded" in types
    # The fake client got the tool result back as a `tool` role message
    fake: FakeChatClient = runtime.chat_client
    tool_messages = [m for m in fake.requests[1]["messages"] if m["role"] == "tool"]
    assert len(tool_messages) == 1
    assert "8450" in tool_messages[0]["content"]


async def test_agent_only_sees_its_own_tools():
    script = [text_response("hello")]
    runtime, store, _ = make_runtime(script)
    await store.set_state("s1", {"status": "active", "current_agent": "triage", "stagnant_turn_count": 0})
    await runtime.run_turn("s1", "hi")

    fake: FakeChatClient = runtime.chat_client
    sent_tools = {t["function"]["name"] for t in fake.requests[0]["tools"]}
    # Triage can only hand off - no business tools, no submit_claim
    assert sent_tools == {"handoff_to_policy", "handoff_to_claims"}


async def test_hallucinated_tool_is_blocked_and_reported():
    script = [
        tool_call_response(("transfer_money", {"amount": 1_000_000})),  # not a registered tool
        text_response("Sorry, I can't do that. Can I help with your policy or a claim?"),
    ]
    runtime, store, bus = make_runtime(script)
    await store.set_state("s1", {"status": "active", "current_agent": "policy", "stagnant_turn_count": 0})

    async with EventRecorder(bus, "s1") as rec:
        result = await runtime.run_turn("s1", "send me money")

    drift = next(e for e in rec.events if e["type"] == "drift_detected")
    assert drift["data"]["kind"] == "hallucinated_tool"
    assert "transfer_money" in drift["data"]["detail"]
    assert result.text.startswith("Sorry")


async def test_tool_exhaustion_returns_spoken_fallback():
    script = [
        tool_call_response(("submit_claim", {"policy_number": "POL-1001", "claim_amount": 1, "documents": []})),
    ]
    runtime, store, bus = make_runtime(script)
    runtime.registry.force_failures("submit_claim", count=99)
    await store.set_state("s1", {"status": "active", "current_agent": "claims", "stagnant_turn_count": 0})

    async with EventRecorder(bus, "s1") as rec:
        result = await runtime.run_turn("s1", "submit it")

    assert "tool_exhausted" in rec.types()
    assert "trouble" in result.text.lower()  # graceful spoken fallback, not a stack trace


async def test_hitl_approval_hook_pauses_instead_of_submitting():
    captured: dict = {}

    async def approval_hook(session_id: str, args: dict) -> int:
        captured["session_id"] = session_id
        captured["args"] = args
        return 42

    script = [
        tool_call_response(
            ("request_claim_approval", {"policy_number": "POL-1001", "claim_amount": 50000,
                                        "documents": ["discharge_summary", "bills", "id_proof"]})
        ),
        text_response("Your claim is queued for human review - I'll confirm shortly."),
    ]
    runtime, store, bus = make_runtime(script, approval_hook=approval_hook)
    await store.set_state("s1", {"status": "active", "current_agent": "claims", "stagnant_turn_count": 0})

    result = await runtime.run_turn("s1", "please submit my claim")

    assert captured["session_id"] == "s1"
    assert captured["args"]["claim_amount"] == 50000
    assert (await store.get_state("s1"))["status"] == "pending_approval"
    assert "review" in result.text


async def test_runaway_tool_loop_is_capped():
    # LLM keeps asking for the same tool forever; runtime must cap iterations
    script = [tool_call_response(("get_hospital_network", {"city": "mumbai"}))] * 20
    runtime, store, bus = make_runtime(script)
    await store.set_state("s1", {"status": "active", "current_agent": "policy", "stagnant_turn_count": 0})

    async with EventRecorder(bus, "s1") as rec:
        result = await runtime.run_turn("s1", "hospitals?")

    fake: FakeChatClient = runtime.chat_client
    assert len(fake.requests) <= runtime.MAX_STEPS_PER_TURN
    drift = next(e for e in rec.events if e["type"] == "drift_detected")
    assert drift["data"]["kind"] == "stagnation"
    assert result.text  # still says something usable to the caller


# ------------------------------------------------- hand-back routing
# Regression: a specialist must not answer off-domain questions with its
# own domain's data. Claims got "how much premium do I owe?" and replied
# with a claim probability - off-domain turns must hand off on THIS turn.


async def test_claims_hands_billing_question_back_to_policy_same_turn():
    script = [
        tool_call_response(("handoff_to_policy", {"reason": "billing question"})),  # claims hands back
        tool_call_response(("get_billing_status", {"policy_number": "POL-2002"})),  # policy takes over
        text_response("You have 8450 rupees due, due on 2026-07-15."),
    ]
    runtime, store, bus = make_runtime(script)
    await store.set_state("s1", {"status": "active", "current_agent": "claims", "stagnant_turn_count": 0})

    async with EventRecorder(bus, "s1") as rec:
        result = await runtime.run_turn("s1", "How much premium do I owe? My policy is POL-2002")

    assert result.agent_name == "policy"
    assert "8450" in result.text
    assert (await store.get_state("s1"))["current_agent"] == "policy"
    handoff = next(e for e in rec.events if e["type"] == "agent_handoff")
    assert (handoff["data"]["from_agent"], handoff["data"]["to_agent"]) == ("claims", "policy")


async def test_specialists_may_hand_back_to_triage():
    # "triage" is a valid handoff target for specialists (not blocked as a
    # hallucinated handoff), so out-of-domain requests can re-enter routing.
    script = [
        tool_call_response(("handoff_to_triage", {"reason": "not a claims topic"})),
        text_response("I can help with policy, billing, or claims. Which one?"),
    ]
    runtime, store, bus = make_runtime(script)
    await store.set_state("s1", {"status": "active", "current_agent": "claims", "stagnant_turn_count": 0})

    async with EventRecorder(bus, "s1") as rec:
        result = await runtime.run_turn("s1", "Can you help me with something else?")

    assert result.agent_name == "triage"
    assert (await store.get_state("s1"))["current_agent"] == "triage"
    assert not any(e["type"] == "drift_detected" for e in rec.events)  # not treated as hallucinated


# The demo brain must implement the same policy with its keyword rules.

def _demo_tools(*names: str) -> list[dict]:
    return [{"type": "function", "function": {"name": n, "description": "", "parameters": {}}} for n in names]


CLAIMS_SYSTEM = {"role": "system", "content": "You are the claims specialist for an insurance call center."}


async def test_demo_claims_agent_routes_billing_question_to_policy():
    from insurance_voice.services.agents.demo_client import DemoChatClient

    resp = await DemoChatClient().chat.completions.create(
        model="demo",
        messages=[CLAIMS_SYSTEM, {"role": "user", "content": "How much premium do I owe? My policy is POL-2002"}],
        tools=_demo_tools("handoff_to_policy", "handoff_to_triage", "calculate_claim_probability"),
    )
    call = resp.choices[0].message.tool_calls[0]
    assert call.function.name == "handoff_to_policy"


async def test_demo_claims_agent_keeps_claim_intent_sentences():
    # "I was hospitalized and want to file a claim" mentions hospital-ish
    # words but IS claim intent - it must stay with claims, never bounce.
    from insurance_voice.services.agents.demo_client import DemoChatClient

    resp = await DemoChatClient().chat.completions.create(
        model="demo",
        messages=[CLAIMS_SYSTEM, {"role": "user", "content": "I was hospitalized and want to file a claim"}],
        tools=_demo_tools("handoff_to_policy", "handoff_to_triage", "calculate_claim_probability"),
    )
    message = resp.choices[0].message
    assert message.tool_calls is None  # asks for the policy number instead
    assert "policy number" in message.content.lower()


async def test_unfulfilled_promise_is_nudged_into_acting():
    """A final reply that only promises action ('let me check... one moment')
    with no tool call leaves dead air on a voice line - the loop must
    push the model to actually call the tool."""
    script = [
        text_response("Got it! Let me check your policy number. One moment."),
        tool_call_response(("get_billing_status", {"policy_number": "POL-1001"})),
        text_response("Your POL-1001 premium is fully paid."),
    ]
    runtime, store, bus = make_runtime(script)
    await store.set_state("s1", {"status": "active", "current_agent": "policy", "stagnant_turn_count": 0})

    async with EventRecorder(bus, "s1") as recorder:
        result = await runtime.run_turn("s1", "What do I owe on my policy?")

    assert result.text == "Your POL-1001 premium is fully paid."
    assert result.tools_used == ["get_billing_status"]
    drift = [e for e in recorder.events if e["type"] == "drift_detected"]
    assert drift and drift[0]["data"]["kind"] == "unfulfilled_promise"


async def test_promise_nudge_fires_only_once_per_turn():
    # If the model promises again after the nudge, let the reply through -
    # never spend more than one corrective LLM call on a turn.
    script = [
        text_response("One moment while I check."),
        text_response("Still checking, hold on."),
    ]
    runtime, store, _ = make_runtime(script)
    await store.set_state("s1", {"status": "active", "current_agent": "policy", "stagnant_turn_count": 0})

    result = await runtime.run_turn("s1", "What do I owe on my policy?")
    assert result.text == "Still checking, hold on."
    assert runtime.chat_client.requests, "script fully consumed"
    assert len(runtime.chat_client.requests) == 2
