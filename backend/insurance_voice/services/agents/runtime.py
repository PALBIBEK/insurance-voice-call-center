"""First-principles agent orchestration loop.

One `run_turn` call = one user utterance processed to completion:
    build messages -> LLM -> (tool calls | handoff | final text) -> loop.

Design notes:
- Handoff is a full control transfer: the target agent restarts the loop
  with its own system prompt and tool set; triage is never re-run within
  the turn. Every handoff is published as an event *before* the new agent
  executes.
- The LLM client is any object exposing `chat.completions.create` - the
  real AsyncOpenAI pointed at OpenRouter in production, a scripted fake
  in tests. This one seam is what keeps the whole suite free of network
  calls and token spend.
- Failure containment: hallucinated tools are blocked and fed back to the
  model as an error message; exhausted tools and runaway loops end the
  turn with a spoken fallback instead of an exception reaching the voice
  channel.
"""

import dataclasses
import datetime
import json
import re
import typing as t

from insurance_voice.services.agents.definitions import AGENTS, AgentDef
from insurance_voice.services.event_bus import EventBus
from insurance_voice.services.session_store import SessionStore
from insurance_voice.services.tools import ToolExhaustedError, ToolRegistry, UnknownToolError


ApprovalHook = t.Callable[[str, dict], t.Awaitable[int]]

TOOL_TROUBLE_FALLBACK = (
    "I'm having trouble reaching that system right now. Let me flag this for a human colleague "
    "to follow up with you - is there anything else I can help with?"
)
LOOP_FALLBACK = (
    "I seem to be going in circles on this one. Let me connect you with a human colleague who can "
    "take it from here."
)

# A final reply that *promises* to check something without any tool call
# strands the caller in dead air ("One moment." ...silence). The prompts
# forbid it, but prompts are advice - this is the enforcement: nudge the
# model once to act, then give up and let the reply through (never burn
# more than one corrective LLM call per turn).
_UNFULFILLED_PROMISE = re.compile(
    r"\b(one (?:moment|second)|just a (?:moment|second|sec)|hold on|bear with me|"
    r"let me (?:check|look|verify|see|pull|find|confirm)|give me a (?:moment|second|minute)|"
    r"i'?ll (?:check|look into|pull up|verify|get back))\b",
    re.IGNORECASE,
)
PROMISE_NUDGE = (
    "You ended your reply promising to check or look something up, but called no tool - the caller "
    "hears dead silence. Call the tool you need RIGHT NOW, or if no tool applies, answer directly or "
    "ask the caller a direct question. Do not promise future action."
)

APPROVAL_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "request_claim_approval",
        "description": (
            "Queue the finalized claim for mandatory human review. Use this instead of submitting "
            "directly - submission only happens after a human approves."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "policy_number": {"type": "string"},
                "claim_amount": {"type": "number"},
                "documents": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["policy_number", "claim_amount", "documents"],
        },
    },
}


@dataclasses.dataclass
class TurnResult:
    text: str
    agent_name: str
    handoffs: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    tools_used: list[str] = dataclasses.field(default_factory=list)
    status: str = "active"


class AgentRuntime:
    MAX_STEPS_PER_TURN = 8  # LLM calls per user utterance - hard cap against runaway loops

    def __init__(
        self,
        *,
        chat_client: t.Any,
        registry: ToolRegistry,
        store: SessionStore,
        bus: EventBus,
        model_triage: str,
        model_specialist: str,
        approval_hook: ApprovalHook | None = None,
    ):
        self.chat_client = chat_client
        self.registry = registry
        self.store = store
        self.bus = bus
        self.model_triage = model_triage
        self.model_specialist = model_specialist
        self.approval_hook = approval_hook

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

    def _tool_specs_for(self, agent: AgentDef) -> list[dict]:
        specs = self.registry.openai_tool_specs(agent.tool_names)
        for target in agent.handoff_targets:
            specs.append(
                {
                    "type": "function",
                    "function": {
                        "name": f"handoff_to_{target}",
                        "description": f"Transfer this conversation to the {target} specialist.",
                        "parameters": {
                            "type": "object",
                            "properties": {"reason": {"type": "string"}},
                            "required": ["reason"],
                        },
                    },
                }
            )
        if agent.can_request_approval:
            specs.append(APPROVAL_TOOL_SPEC)
        return specs

    def _model_for(self, agent: AgentDef) -> str:
        return self.model_triage if agent.model_tier == "triage" else self.model_specialist

    async def run_turn(self, session_id: str, user_text: str) -> TurnResult:
        """NOTE: transcript (turn_created) events are the gateway's job -
        it persists the turn rows first and publishes after commit, so a
        client can never observe an event for a row that isn't readable yet."""
        state = await self.store.get_state(session_id)
        agent = AGENTS[state.get("current_agent", "triage")]

        await self.store.append_history(session_id, {"role": "user", "content": user_text})

        history = await self.store.get_history(session_id)
        # history already includes the user message appended above
        messages: list[dict] = [{"role": "system", "content": agent.system_prompt}, *history]

        result = await self._run_loop(session_id, agent, messages)

        await self.store.append_history(session_id, {"role": "assistant", "content": result.text})
        return result

    async def _persist_tool_exchange(self, session_id: str, call: t.Any, result_json: str) -> None:
        """Durably record an executed tool call + its result in the session
        history as an adjacent assistant/tool pair. Without this the next
        turn's LLM forgets what tools returned (e.g. the caller's policy
        number from get_caller_profile) and hallucinates values."""
        await self.store.append_history(
            session_id,
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.function.name, "arguments": call.function.arguments},
                    }
                ],
            },
        )
        await self.store.append_history(
            session_id, {"role": "tool", "tool_call_id": call.id, "content": result_json}
        )

    async def _run_loop(self, session_id: str, agent: AgentDef, messages: list[dict]) -> TurnResult:
        handoffs: list[tuple[str, str]] = []
        tools_used: list[str] = []
        status = "active"
        promise_nudged = False

        async def observer(event_type: str, data: dict) -> None:
            await self._emit(session_id, event_type, data)

        for _step in range(self.MAX_STEPS_PER_TURN):
            response = await self.chat_client.chat.completions.create(
                model=self._model_for(agent),
                messages=messages,
                tools=self._tool_specs_for(agent),
            )
            message = response.choices[0].message

            if not message.tool_calls:
                text = message.content or ""
                if not promise_nudged and _UNFULFILLED_PROMISE.search(text):
                    promise_nudged = True
                    await self._emit(
                        session_id,
                        "drift_detected",
                        {"kind": "unfulfilled_promise",
                         "detail": f"agent ended turn promising action without a tool call: {text[:80]!r}",
                         "action": "nudged"},
                    )
                    messages.append({"role": "assistant", "content": text})
                    messages.append({"role": "system", "content": PROMISE_NUDGE})
                    continue
                return TurnResult(
                    text=text, agent_name=agent.name, handoffs=handoffs,
                    tools_used=tools_used, status=status,
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.function.name, "arguments": c.function.arguments},
                        }
                        for c in message.tool_calls
                    ],
                }
            )

            for call in message.tool_calls:
                name = call.function.name
                arguments = json.loads(call.function.arguments or "{}")

                if name.startswith("handoff_to_"):
                    target = name.removeprefix("handoff_to_")
                    if target in AGENTS and target in agent.handoff_targets:
                        await self._emit(
                            session_id,
                            "agent_handoff",
                            {"from_agent": agent.name, "to_agent": target, "reason": arguments.get("reason", "")},
                        )
                        handoffs.append((agent.name, target))
                        await self.store.update_state(session_id, current_agent=target)
                        agent = AGENTS[target]
                        # Full control transfer: fresh context under the new
                        # agent's prompt; drop the in-flight tool exchange.
                        history = await self.store.get_history(session_id)
                        messages = [{"role": "system", "content": agent.system_prompt}, *history]
                        break  # remaining tool calls belonged to the old agent
                    await self._emit(
                        session_id,
                        "drift_detected",
                        {"kind": "hallucinated_tool", "detail": f"invalid handoff target in {name}", "action": "blocked"},
                    )
                    messages.append(self._tool_error_message(call.id, f"Unknown handoff {name}"))
                    continue

                if name == "request_claim_approval" and agent.can_request_approval:
                    tools_used.append(name)
                    if "probability" not in arguments:
                        # carry the last computed score into the approval card
                        state = await self.store.get_state(session_id)
                        if state.get("last_claim_probability") is not None:
                            arguments["probability"] = state["last_claim_probability"]
                    approval_id = None
                    if self.approval_hook is not None:
                        approval_id = await self.approval_hook(session_id, arguments)
                    await self.store.update_state(session_id, status="pending_approval")
                    status = "pending_approval"
                    approval_result = json.dumps(
                        {"queued_for_review": True, "approval_id": approval_id,
                         "note": "Tell the caller the claim is queued for human review."}
                    )
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": approval_result})
                    await self._persist_tool_exchange(session_id, call, approval_result)
                    continue

                tools_used.append(name)
                try:
                    tool_result = await self.registry.invoke(name, arguments, session_id=session_id, observer=observer)
                except UnknownToolError:
                    await self._emit(
                        session_id,
                        "drift_detected",
                        {"kind": "hallucinated_tool", "detail": f"agent requested unregistered tool {name}", "action": "blocked"},
                    )
                    messages.append(
                        self._tool_error_message(
                            call.id, f"Tool {name!r} does not exist. Stay within your available tools."
                        )
                    )
                    continue
                except ToolExhaustedError:
                    # Registry already emitted tool_exhausted; end the turn
                    # with a graceful spoken fallback instead of retry-looping.
                    return TurnResult(
                        text=TOOL_TROUBLE_FALLBACK, agent_name=agent.name, handoffs=handoffs,
                        tools_used=tools_used, status=status,
                    )

                if name == "calculate_claim_probability" and isinstance(tool_result.output, dict):
                    probability = tool_result.output.get("probability")
                    if probability is not None:
                        await self.store.update_state(session_id, last_claim_probability=probability)

                result_json = json.dumps(tool_result.output)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result_json})
                await self._persist_tool_exchange(session_id, call, result_json)

        await self._emit(
            session_id,
            "drift_detected",
            {"kind": "stagnation", "detail": f"turn exceeded {self.MAX_STEPS_PER_TURN} steps", "action": "escalated"},
        )
        return TurnResult(
            text=LOOP_FALLBACK, agent_name=agent.name, handoffs=handoffs, tools_used=tools_used, status=status
        )

    @staticmethod
    def _tool_error_message(tool_call_id: str, error: str) -> dict:
        return {"role": "tool", "tool_call_id": tool_call_id, "content": json.dumps({"error": error})}
