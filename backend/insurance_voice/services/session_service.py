"""Call session lifecycle + read models (transcript, trace)."""

import datetime
import uuid

import sqlalchemy as sa

from insurance_voice.db.models import AgentTurnLog, CallSession, TraceSpan, Turn
from insurance_voice.db.session import Database
from insurance_voice.services.event_bus import EventBus
from insurance_voice.services.session_store import SessionStore


class SessionNotFoundError(LookupError):
    pass


class SessionService:
    def __init__(self, *, db: Database, store: SessionStore, bus: EventBus):
        self.db = db
        self.store = store
        self.bus = bus

    async def create_session(self, channel: str, user_id: str = "anonymous") -> dict:
        session_id = str(uuid.uuid4())
        async with self.db.session() as s:
            s.add(CallSession(id=session_id, channel=channel, user_id=user_id))
            await s.commit()
        await self.store.set_state(
            session_id,
            {"status": "active", "current_agent": "triage", "stagnant_turn_count": 0, "offdomain_turn_count": 0},
        )
        return {"session_id": session_id, "status": "active", "ws_url": f"/ws/sessions/{session_id}"}

    async def list_sessions(self, user_id: str, limit: int = 50) -> list[dict]:
        """The user's conversations, newest first - powers the history sidebar."""
        async with self.db.session() as s:
            rows = (
                await s.execute(
                    sa.select(CallSession)
                    .where(CallSession.user_id == user_id)
                    .order_by(CallSession.created_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
            # One-line preview per session: the first user turn, if any
            previews: dict[str, str] = {}
            if rows:
                first_turns = (
                    await s.execute(
                        sa.select(Turn.session_id, sa.func.min(Turn.id).label("first_id"))
                        .where(Turn.session_id.in_([r.id for r in rows]), Turn.role == "user")
                        .group_by(Turn.session_id)
                    )
                ).all()
                if first_turns:
                    id_map = {t.first_id: t.session_id for t in first_turns}
                    contents = (
                        await s.execute(sa.select(Turn.id, Turn.content).where(Turn.id.in_(list(id_map))))
                    ).all()
                    previews = {id_map[c.id]: c.content for c in contents}
        return [
            {
                "session_id": r.id,
                "status": r.status,
                "current_agent": r.current_agent,
                "channel": r.channel,
                "started_at": r.created_at.isoformat(),
                "preview": previews.get(r.id, ""),
            }
            for r in rows
        ]

    async def get_session(self, session_id: str) -> dict:
        async with self.db.session() as s:
            row = await s.get(CallSession, session_id)
        if row is None:
            raise SessionNotFoundError(session_id)
        return {
            "session_id": row.id,
            "status": row.status,
            "current_agent": row.current_agent,
            "channel": row.channel,
            "started_at": row.created_at.isoformat(),
        }

    async def end_session(self, session_id: str) -> dict:
        async with self.db.session() as s:
            row = await s.get(CallSession, session_id)
            if row is None:
                raise SessionNotFoundError(session_id)
            row.status = "completed"
            row.ended_at = datetime.datetime.now(datetime.timezone.utc)
            await s.commit()
        await self.store.update_state(session_id, status="completed")
        await self.bus.publish(
            session_id,
            {
                "type": "session_ended",
                "session_id": session_id,
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "data": {"reason": "ended by user"},
            },
        )
        return {"session_id": session_id, "status": "completed"}

    async def list_turns(self, session_id: str) -> list[dict]:
        async with self.db.session() as s:
            turns = (
                await s.scalars(sa.select(Turn).where(Turn.session_id == session_id).order_by(Turn.id))
            ).all()
        return [
            {"role": t.role, "agent_name": t.agent_name, "content": t.content, "created_at": t.created_at.isoformat()}
            for t in turns
        ]

    async def list_agent_log(self, session_id: str) -> list[dict]:
        """The session's agent_turn_log, ordered - a step-by-step replay of
        what the system did (messages, tool calls, handoffs, HITL)."""
        async with self.db.session() as s:
            rows = (
                await s.scalars(
                    sa.select(AgentTurnLog)
                    .where(AgentTurnLog.session_id == session_id)
                    .order_by(AgentTurnLog.turn_step)
                )
            ).all()
        return [
            {
                "turn_step": r.turn_step,
                "user_id": r.user_id,
                "step_type": r.step_type,
                "agent_name": r.agent_name,
                "message": r.message,
                "tool_name": r.tool_name,
                "tool_args": r.tool_args,
                "tool_status": r.tool_status,
                "latency_ms": r.latency_ms,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]

    async def tool_metrics(self) -> list[dict]:
        """Per-tool aggregates over every recorded tool attempt: attempts,
        success rate, average latency of successful calls."""
        async with self.db.session() as s:
            rows = (
                await s.execute(
                    sa.select(
                        AgentTurnLog.tool_name,
                        sa.func.count().label("attempts"),
                        sa.func.sum(sa.case((AgentTurnLog.tool_status == "succeeded", 1), else_=0)).label("succeeded"),
                        sa.func.avg(
                            sa.case((AgentTurnLog.tool_status == "succeeded", AgentTurnLog.latency_ms))
                        ).label("avg_latency_ms"),
                    )
                    .where(AgentTurnLog.step_type == "tool_call", AgentTurnLog.tool_name.is_not(None))
                    .group_by(AgentTurnLog.tool_name)
                    .order_by(AgentTurnLog.tool_name)
                )
            ).all()
        return [
            {
                "tool_name": r.tool_name,
                "attempts": r.attempts,
                "succeeded": r.succeeded or 0,
                "success_rate": round((r.succeeded or 0) / r.attempts, 3) if r.attempts else None,
                "avg_latency_ms": round(r.avg_latency_ms, 1) if r.avg_latency_ms is not None else None,
            }
            for r in rows
        ]

    async def get_trace(self, session_id: str) -> list[dict]:
        async with self.db.session() as s:
            spans = (
                await s.scalars(sa.select(TraceSpan).where(TraceSpan.session_id == session_id).order_by(TraceSpan.id))
            ).all()
        return [
            {
                "span_id": sp.span_id,
                "parent_span_id": sp.parent_span_id,
                "kind": sp.kind,
                "payload": sp.payload,
                "created_at": sp.created_at.isoformat(),
            }
            for sp in spans
        ]
