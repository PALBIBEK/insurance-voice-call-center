"""Custom trace exporter: every published event also becomes durable rows.

Wrapping the event bus (instead of instrumenting each service) gives one
choke point where the entire execution flow - agent runs, tool calls,
handoffs, guardrails, HITL - is durably recorded, no vendor SDK involved.

Two projections are written per event:
- trace_span: raw span per event (developer-facing trace tree)
- agent_turn_log: flat ordered step log per session (analytics +
  step-by-step reconstruction; see AgentTurnLog docstring)
"""

import typing as t
import uuid

import sqlalchemy as sa

from insurance_voice.db.models import AgentTurnLog, CallSession, TraceSpan
from insurance_voice.db.session import Database
from insurance_voice.services.event_bus import EventBus


_KIND_BY_EVENT = {
    "turn_created": "agent_run",
    "agent_handoff": "handoff",
    "tool_call_started": "tool_call",
    "tool_call_succeeded": "tool_call",
    "tool_call_failed": "tool_call",
    "tool_exhausted": "tool_call",
    "drift_detected": "guardrail",
    "approval_required": "hitl",
    "approval_decided": "hitl",
    "claim_submitted": "hitl",
    "session_ended": "system",
}

_TOOL_STATUS_BY_EVENT = {
    "tool_call_succeeded": "succeeded",
    "tool_call_failed": "failed",
    "tool_exhausted": "exhausted",
}


def _turn_log_fields(event_type: str, data: dict) -> dict | None:
    """Map an event to agent_turn_log columns; None = not a loggable step.
    tool_call_started is skipped - the outcome events carry the same
    arguments plus status/latency, and per-attempt rows come from
    tool_call_failed being emitted once per failed attempt."""
    if event_type == "turn_created":
        role = data.get("role")
        return {
            "step_type": "user_message" if role == "user" else "agent_message",
            "agent_name": data.get("agent_name"),
            "message": data.get("content"),
        }
    if event_type == "agent_handoff":
        return {
            "step_type": "handoff",
            "agent_name": data.get("to_agent"),
            "message": f"{data.get('from_agent')} -> {data.get('to_agent')}: {data.get('reason', '')}",
        }
    if event_type in _TOOL_STATUS_BY_EVENT:
        return {
            "step_type": "tool_call",
            "tool_name": data.get("tool_name"),
            "tool_args": data.get("arguments"),
            "tool_status": _TOOL_STATUS_BY_EVENT[event_type],
            "latency_ms": data.get("latency_ms"),
            "message": data.get("error"),
        }
    if event_type == "approval_required":
        return {"step_type": "approval_requested", "message": str(data.get("claim_draft", ""))}
    if event_type == "approval_decided":
        return {"step_type": "approval_decided", "message": f"{data.get('status')} by {data.get('decided_by')}"}
    if event_type == "claim_submitted":
        return {"step_type": "claim_submitted", "message": f"{data.get('status')} ref={data.get('reference')}"}
    if event_type == "drift_detected":
        return {"step_type": "guardrail", "message": f"{data.get('kind')}: {data.get('action')}"}
    return None


class RecordingEventBus:
    """EventBus decorator: persist span + turn-log row, then delegate."""

    def __init__(self, inner: EventBus, db: Database):
        self._inner = inner
        self._db = db
        self._user_by_session: dict[str, str] = {}

    async def _resolve_user(self, s, session_id: str) -> str:
        cached = self._user_by_session.get(session_id)
        if cached is not None:
            return cached
        call = await s.get(CallSession, session_id)
        user_id = call.user_id if call is not None else "anonymous"
        self._user_by_session[session_id] = user_id
        return user_id

    async def publish(self, session_id: str, event: dict) -> None:
        event_type = event.get("type", "")
        kind = _KIND_BY_EVENT.get(event_type, "system")
        turn_fields = _turn_log_fields(event_type, event.get("data", {}))
        async with self._db.session() as s:
            s.add(
                TraceSpan(
                    session_id=session_id,
                    span_id=str(uuid.uuid4()),
                    kind=kind,
                    payload={"event": event_type, **event.get("data", {})},
                )
            )
            if turn_fields is not None:
                user_id = await self._resolve_user(s, session_id)
                next_step = (
                    await s.scalar(
                        sa.select(sa.func.coalesce(sa.func.max(AgentTurnLog.turn_step), 0)).where(
                            AgentTurnLog.session_id == session_id
                        )
                    )
                ) + 1
                s.add(AgentTurnLog(session_id=session_id, user_id=user_id, turn_step=next_step, **turn_fields))
            await s.commit()
        await self._inner.publish(session_id, event)

    def subscribe(self, session_id: str) -> t.AsyncContextManager:
        return self._inner.subscribe(session_id)
