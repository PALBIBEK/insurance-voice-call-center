"""Drift detection: off-domain turns and cross-turn stagnation.

(The third guardrail - hallucinated tools - lives in the runtime loop and
is covered in test_agents.py.)
"""

from insurance_voice.services.drift_service import DriftService
from insurance_voice.services.event_bus import InMemoryEventBus
from insurance_voice.services.session_store import InMemorySessionStore

from tests.fakes import FakeChatClient, text_response


def make_service(max_offdomain=2, max_stagnant=3, chat_client=None):
    store = InMemorySessionStore()
    bus = InMemoryEventBus()
    service = DriftService(
        store=store, bus=bus, max_offdomain_turns=max_offdomain, max_stagnant_turns=max_stagnant,
        chat_client=chat_client, model="test-model",
    )
    return service, store, bus


async def test_insurance_utterances_pass_domain_check():
    service, _, _ = make_service()
    for text in [
        "I want to check my billing status",
        "Is Lilavati hospital covered under cashless?",
        "I need to file a claim for my hospitalization",
        "What documents do I need for reimbursement?",
        "my policy number is POL-1001",
        "yes",  # short confirmations must never be flagged
        "50000 rupees",
    ]:
        assert await service.is_on_domain(text), text


async def test_clearly_offdomain_utterances_fail_domain_check():
    service, _, _ = make_service()
    for text in [
        "write me a poem about the moon",
        "what's the best pizza place nearby",
        "help me with my python homework",
    ]:
        assert not await service.is_on_domain(text), text


async def test_llm_classifier_understands_typos_keywords_would_miss():
    """'calim' has no domain keyword, but the prompt-based gate still reads
    it as a claim question."""
    service, _, _ = make_service(chat_client=FakeChatClient([text_response("ON")]))
    assert await service.is_on_domain("Why can't I calim for new?")


async def test_llm_classifier_off_verdict_redirects():
    service, store, _ = make_service(chat_client=FakeChatClient([text_response("OFF")]))
    await store.set_state("s1", {"offdomain_turn_count": 0})
    verdict = await service.check_user_turn("s1", "what's a good biryani recipe for tonight")
    assert verdict.action == "redirected"


async def test_llm_classifier_failure_fails_open():
    """A broken topic filter must never block a caller."""
    service, _, _ = make_service(chat_client=FakeChatClient([]))  # empty script -> raises
    assert await service.is_on_domain("some longer message with no insurance words at all")


async def test_offdomain_redirects_then_escalates():
    service, store, _ = make_service(max_offdomain=2)
    await store.set_state("s1", {"offdomain_turn_count": 0})

    verdict = await service.check_user_turn("s1", "tell me a joke")
    assert verdict.action == "redirected"
    assert verdict.reply  # a gentle spoken redirect the gateway can use

    verdict = await service.check_user_turn("s1", "come on, just one joke")
    assert verdict.action == "escalated"


async def test_ondomain_turn_resets_offdomain_counter():
    service, store, _ = make_service(max_offdomain=2)
    await store.set_state("s1", {"offdomain_turn_count": 0})

    await service.check_user_turn("s1", "tell me a joke")
    verdict = await service.check_user_turn("s1", "ok fine, what's my billing status")
    assert verdict.action == "ok"
    assert (await store.get_state("s1"))["offdomain_turn_count"] == 0


async def test_stagnation_counter_and_forced_reset():
    service, store, bus = make_service(max_stagnant=3)
    await store.set_state("s1", {"current_agent": "claims", "stagnant_turn_count": 0})

    # Same agent + same fingerprint three turns in a row -> reset to triage
    async with bus.subscribe("s1") as queue:
        r1 = await service.track_turn_progress("s1", agent_name="claims", fingerprint="ask_docs")
        r2 = await service.track_turn_progress("s1", agent_name="claims", fingerprint="ask_docs")
        r3 = await service.track_turn_progress("s1", agent_name="claims", fingerprint="ask_docs")
        assert (r1, r2) == ("ok", "ok")
        assert r3 == "reset_to_triage"
        state = await store.get_state("s1")
        assert state["current_agent"] == "triage"
        assert state["stagnant_turn_count"] == 0
        # both a drift event and a handoff event fire, so the UI rail updates
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
    types = [e["type"] for e in events]
    assert "drift_detected" in types
    assert "agent_handoff" in types


async def test_progress_resets_stagnation():
    service, store, _ = make_service(max_stagnant=3)
    await store.set_state("s1", {"current_agent": "claims", "stagnant_turn_count": 0})

    await service.track_turn_progress("s1", agent_name="claims", fingerprint="ask_docs")
    await service.track_turn_progress("s1", agent_name="claims", fingerprint="got_docs")  # new fingerprint = progress
    state = await store.get_state("s1")
    assert state["stagnant_turn_count"] == 1  # restarted counting from the new fingerprint


async def test_classifier_sees_agent_context_for_followups():
    """A short follow-up ('Have you checked it?') has no domain keywords on
    its own - the classifier must see the agent's last line and judge
    the exchange, not the utterance in isolation."""
    fake = FakeChatClient([text_response("ON")])
    service, store, _ = make_service(chat_client=fake)
    await store.append_history("s1", {"role": "user", "content": "I need to file a claim"})
    await store.append_history("s1", {"role": "assistant", "content": "Let me check your policy number."})

    verdict = await service.check_user_turn("s1", "Have you checked it already or not?")

    assert verdict.action == "ok"
    sent = fake.requests[0]["messages"][1]["content"]
    assert "Let me check your policy number." in sent
    assert "Have you checked it already or not?" in sent
