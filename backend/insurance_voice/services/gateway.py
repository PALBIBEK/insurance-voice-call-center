"""Channel-agnostic turn handling.

Both entry surfaces - the ElevenLabs custom-LLM webhook and the frontend
WebSocket's text mode - land on handle_turn with (session_id, text) and
get back the reply text. Guardrails run before the agent; transcript
persistence and stagnation tracking run after; neither entry surface
knows any of that happens.
"""

import datetime

import sqlalchemy as sa

from insurance_voice.db.models import CallSession, Turn
from insurance_voice.db.session import Database
from insurance_voice.services.agents.runtime import AgentRuntime
from insurance_voice.services.drift_service import DriftService
from insurance_voice.services.event_bus import EventBus
from insurance_voice.services.session_service import SessionNotFoundError
from insurance_voice.services.session_store import SessionStore


class GatewayService:
    def __init__(
        self,
        *,
        db: Database,
        store: SessionStore,
        bus: EventBus,
        runtime: AgentRuntime,
        drift: DriftService,
    ):
        self.db = db
        self.store = store
        self.bus = bus
        self.runtime = runtime
        self.drift = drift

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

    async def _persist_user_turn(self, session_id: str, user_text: str) -> None:
        async with self.db.session() as s:
            s.add(Turn(session_id=session_id, role="user", content=user_text))
            await s.commit()

    async def _persist_agent_turn(self, session_id: str, agent_name: str, reply: str) -> None:
        async with self.db.session() as s:
            s.add(Turn(session_id=session_id, role="agent", agent_name=agent_name, content=reply))
            await s.commit()

    async def _sync_call_session_row(self, session_id: str) -> None:
        state = await self.store.get_state(session_id)
        async with self.db.session() as s:
            row = await s.get(CallSession, session_id)
            if row is not None:
                row.current_agent = state.get("current_agent", row.current_agent)
                row.status = state.get("status", row.status)
                await s.commit()

    async def rehydrate_state(self, session_id: str) -> dict:
        """Rebuild hot session state from the durable rows. The session
        store is a cache (in-memory locally, Redis in compose); after a
        backend restart it is empty, but call_session + turn never forgot.
        Without this, reopening an old conversation - including one parked
        in pending_approval - dies with SessionNotFoundError."""
        async with self.db.session() as s:
            row = await s.get(CallSession, session_id)
            if row is None:
                return {}
            turns = (
                await s.scalars(sa.select(Turn).where(Turn.session_id == session_id).order_by(Turn.id))
            ).all()
        state = {
            "status": row.status,
            "current_agent": row.current_agent,
            "stagnant_turn_count": 0,
            "offdomain_turn_count": 0,
        }
        await self.store.set_state(session_id, state)
        # LLM context: the spoken transcript. Tool exchanges are not in the
        # turn table - agents refetch what they need (profile, claim status).
        for turn in turns:
            role = "user" if turn.role == "user" else "assistant"
            await self.store.append_history(session_id, {"role": role, "content": turn.content})
        return state

    async def handle_turn(self, session_id: str, user_text: str) -> str:
        """Persist-then-publish at every step: a client reacting to a
        turn_created event must always be able to read that row already."""
        state = await self.store.get_state(session_id)
        if not state:
            state = await self.rehydrate_state(session_id)
        if not state:
            raise SessionNotFoundError(session_id)

        await self._persist_user_turn(session_id, user_text)
        await self._emit(session_id, "turn_created", {"role": "user", "agent_name": None, "content": user_text})

        verdict = await self.drift.check_user_turn(session_id, user_text)
        if verdict.action != "ok":
            # Guardrail reply without touching the LLM at all
            agent_name = state.get("current_agent", "triage")
            await self._persist_agent_turn(session_id, agent_name, verdict.reply)
            await self._emit(
                session_id, "turn_created", {"role": "agent", "agent_name": agent_name, "content": verdict.reply}
            )
            return verdict.reply

        result = await self.runtime.run_turn(session_id, user_text)
        await self._persist_agent_turn(session_id, result.agent_name, result.text)

        # Stagnation: same agent doing the same thing turn after turn.
        # A pending approval is progress by definition - skip tracking.
        if result.status != "pending_approval":
            fingerprint = "|".join(result.tools_used) or f"said:{result.text[:60]}"
            await self.drift.track_turn_progress(session_id, agent_name=result.agent_name, fingerprint=fingerprint)

        await self._sync_call_session_row(session_id)
        await self._emit(
            session_id, "turn_created", {"role": "agent", "agent_name": result.agent_name, "content": result.text}
        )
        return result.text
