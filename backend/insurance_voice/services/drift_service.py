
"""Drift detection.

Two checks live here (the third, hallucinated tools, is enforced inline in
the runtime loop where the tool call is intercepted):

- Off-domain: prompt-based - a one-word ON/OFF classification by the cheap
  triage-tier model, so misspellings and paraphrases are understood the
  way a human would ("calim" is a claim, not chit-chat). Short turns
  ("yes", numbers, policy ids) always pass without any call. The filter
  fails OPEN: if the classifier errors, the caller is never blocked.
  Zero-key demo mode (no LLM at all) falls back to a keyword allowlist.
- Stagnation: a per-session fingerprint of "what the agent did this turn";
  the same fingerprint from the same agent N turns running forces a reset
  to triage, published as a normal agent_handoff so the UI needs no
  special case.
"""

import dataclasses
import datetime
import re

from insurance_voice.services.event_bus import EventBus
from insurance_voice.services.session_store import SessionStore


_DOMAIN_TERMS = re.compile(
    r"\b(insur\w*|polic\w*|claim\w*|premium\w*|bill\w*|hospital\w*|cashless|reimburs\w*|"
    r"cover\w*|document\w*|discharge|treatment|medical|health|accident|surgery|"
    r"pol-\d+|clm-\w+|submit\w*|approv\w*|network|due|paid|lapse\w*|renew\w*|sum insured)\b",
    re.IGNORECASE,
)

# Turns this short are confirmations/answers to the agent's question
# ("yes", "50000 rupees", "POL-1001") - never treat them as drift.
_MAX_WORDS_ALWAYS_OK = 3

_CLASSIFIER_PROMPT = (
    "You are the topic gate for an insurance call center voice line. The caller may discuss "
    "policies, coverage, premiums, billing, network hospitals, claims, documents, approvals - "
    "and anything supporting that call: greetings, confirmations, thanks, numbers, ids, "
    "complaints or questions about the process, and misspelled versions of any of these. "
    "You may be shown the agent's last line for context: a reply that answers or follows up "
    "on it ('have you checked it?', 'yes do that') is ON topic. "
    "All of that is ON topic. Only clearly unrelated requests (weather, jokes, homework, "
    "recipes, general chit-chat) are OFF topic. Answer with exactly one word: ON or OFF."
)

REDIRECT_REPLY = (
    "I can only help with insurance matters - your policy, billing, network hospitals, or claims. "
    "What can I help you with there?"
)


@dataclasses.dataclass(frozen=True)
class DriftVerdict:
    action: str  # "ok" | "redirected" | "escalated"
    reply: str = ""


class DriftService:
    def __init__(
        self,
        *,
        store: SessionStore,
        bus: EventBus,
        max_offdomain_turns: int,
        max_stagnant_turns: int,
        chat_client: object | None = None,  # LLM classifier; None -> keyword fallback (zero-key demo)
        model: str = "",
    ):
        self.store = store
        self.bus = bus
        self.max_offdomain_turns = max_offdomain_turns
        self.max_stagnant_turns = max_stagnant_turns
        self.chat_client = chat_client
        self.model = model

    async def _emit(self, session_id: str, event_type: str, data: dict) -> None:
        await self.bus.publish(
            session_id,
            {
                "type": event_type,
                "session_id": session_id,
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "data": data,
            },
        )

    async def is_on_domain(self, text: str, agent_context: str = "") -> bool:
        if len(text.split()) <= _MAX_WORDS_ALWAYS_OK:
            return True
        if self.chat_client is not None:
            # A follow-up like "have you checked it?" is only judgeable
            # against what the agent just said - classify the exchange,
            # not the utterance in isolation.
            content = (
                f"Agent's last line: {agent_context}\nCaller's reply: {text}" if agent_context else text
            )
            try:
                response = await self.chat_client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": _CLASSIFIER_PROMPT},
                        {"role": "user", "content": content},
                    ],
                )
                verdict = (response.choices[0].message.content or "").strip().upper()
                return not verdict.startswith("OFF")
            except Exception:
                # Fail open: a broken topic filter must never block a caller.
                return True
        # Keyword fallback stays context-blind on purpose: agent lines almost
        # always contain domain terms, so matching against them would wave
        # every off-topic reply through in zero-key demo mode.
        return _DOMAIN_TERMS.search(text) is not None

    async def check_user_turn(self, session_id: str, text: str) -> DriftVerdict:
        """Run before the turn reaches the agent. ok -> proceed;
        redirected -> speak `reply` instead of running the agent;
        escalated -> hand the caller to a human."""
        state = await self.store.get_state(session_id)
        count = int(state.get("offdomain_turn_count", 0))

        history = await self.store.get_history(session_id)
        agent_context = next(
            (m.get("content") or "" for m in reversed(history) if m.get("role") == "assistant" and m.get("content")),
            "",
        )
        # Last 300 chars are plenty for anaphora; the gate runs every turn,
        # so its token bill must stay near-zero.
        if await self.is_on_domain(text, agent_context=agent_context[-300:]):
            if count:
                await self.store.update_state(session_id, offdomain_turn_count=0)
            return DriftVerdict(action="ok")

        count += 1
        await self.store.update_state(session_id, offdomain_turn_count=count)
        if count >= self.max_offdomain_turns:
            await self._emit(
                session_id,
                "drift_detected",
                {"kind": "off_domain", "detail": f"{count} consecutive off-domain turns", "action": "escalated"},
            )
            return DriftVerdict(
                action="escalated",
                reply="This seems outside what I can help with. Let me connect you with a human colleague.",
            )

        await self._emit(
            session_id,
            "drift_detected",
            {"kind": "off_domain", "detail": "off-domain turn", "action": "redirected"},
        )
        return DriftVerdict(action="redirected", reply=REDIRECT_REPLY)

    async def track_turn_progress(self, session_id: str, *, agent_name: str, fingerprint: str) -> str:
        """Call after each completed turn with a fingerprint of what happened
        (agent + tools used + status). Returns "ok" or "reset_to_triage"."""
        state = await self.store.get_state(session_id)
        previous = state.get("last_turn_fingerprint")
        current = f"{agent_name}:{fingerprint}"
        count = int(state.get("stagnant_turn_count", 0)) + 1 if previous == current else 1

        if count >= self.max_stagnant_turns:
            await self._emit(
                session_id,
                "drift_detected",
                {"kind": "stagnation", "detail": f"no progress for {count} turns", "action": "reset_to_triage"},
            )
            await self._emit(
                session_id,
                "agent_handoff",
                {"from_agent": agent_name, "to_agent": "triage", "reason": "stagnation reset"},
            )
            await self.store.update_state(
                session_id, current_agent="triage", stagnant_turn_count=0, last_turn_fingerprint=None
            )
            return "reset_to_triage"

        await self.store.update_state(session_id, stagnant_turn_count=count, last_turn_fingerprint=current)
        return "ok"
