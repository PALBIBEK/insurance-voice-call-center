"""Human-in-the-loop gate around claim submission.

Two call stacks, joined only by the approval_request row and the event
bus: the agent's request_claim_approval lands here and pauses the
session; a human's REST decision lands here and - only on approval -
fires the real submit_claim through the normal tool wrapper (latency,
failure injection and retries all still apply).
"""

import datetime
import typing as t

import sqlalchemy as sa

from insurance_voice.db.models import ApprovalRequest, CallSession, ClaimDraft, ClaimSubmission, UserInfo
from insurance_voice.db.session import Database
from insurance_voice.services.event_bus import EventBus
from insurance_voice.services.session_store import SessionStore
from insurance_voice.services.tools import ToolExhaustedError, ToolRegistry


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class ApprovalService:
    def __init__(self, *, db: Database, registry: ToolRegistry, store: SessionStore, bus: EventBus):
        self.db = db
        self.registry = registry
        self.store = store
        self.bus = bus

    async def _emit(self, session_id: str, event_type: str, data: dict) -> None:
        await self.bus.publish(
            session_id,
            {"type": event_type, "session_id": session_id, "ts": _utcnow().isoformat(), "data": data},
        )

    async def request_approval(self, session_id: str, claim_args: dict) -> int:
        """Called by the agent runtime's approval hook. Persists the draft +
        pending approval, notifies the frontend, returns the approval id.

        Idempotent per session: if a pending approval already exists (e.g.
        the agent re-queues after the caller asks about the claim again),
        the existing one is re-announced instead of duplicated."""
        async with self.db.session() as s:
            existing = (
                await s.execute(
                    sa.select(ApprovalRequest, ClaimDraft)
                    .join(ClaimDraft, ApprovalRequest.claim_draft_id == ClaimDraft.id)
                    .where(ApprovalRequest.session_id == session_id, ApprovalRequest.status == "pending")
                )
            ).first()
        if existing is not None:
            approval, draft = existing
            await self._emit(
                session_id,
                "approval_required",
                {
                    "approval_id": approval.id,
                    "claim_draft": {
                        "policy_number": draft.policy_number,
                        "claim_amount": draft.claim_amount,
                        "documents": draft.documents,
                        "probability": draft.probability,
                    },
                    "probability_score": draft.probability,
                },
            )
            return approval.id

        async with self.db.session() as s:
            draft = ClaimDraft(
                session_id=session_id,
                policy_number=str(claim_args.get("policy_number", "")),
                claim_amount=float(claim_args.get("claim_amount", 0)),
                documents=list(claim_args.get("documents", [])),
                probability=claim_args.get("probability"),
            )
            s.add(draft)
            await s.flush()
            approval = ApprovalRequest(session_id=session_id, claim_draft_id=draft.id)
            s.add(approval)
            await s.flush()
            approval_id = approval.id
            draft_payload = {
                "policy_number": draft.policy_number,
                "claim_amount": draft.claim_amount,
                "documents": draft.documents,
                "probability": draft.probability,
            }
            await s.commit()

        await self._emit(
            session_id,
            "approval_required",
            {"approval_id": approval_id, "claim_draft": draft_payload, "probability_score": draft_payload["probability"]},
        )
        return approval_id

    async def list_approvals(self, status: str = "pending") -> list[dict]:
        """Approval tasks for the reviewer portal. status: pending (the live
        queue), decided (durable history - survives tab restarts), or all."""
        query = (
            sa.select(ApprovalRequest, ClaimDraft, CallSession.user_id, UserInfo.user_data, ClaimSubmission.reference)
            .join(ClaimDraft, ApprovalRequest.claim_draft_id == ClaimDraft.id)
            .join(CallSession, ApprovalRequest.session_id == CallSession.id)
            .outerjoin(UserInfo, UserInfo.user_id == CallSession.user_id)
            .outerjoin(ClaimSubmission, ClaimSubmission.claim_draft_id == ClaimDraft.id)
            .order_by(ApprovalRequest.created_at)
        )
        if status == "pending":
            query = query.where(ApprovalRequest.status == "pending")
        elif status == "decided":
            query = query.where(ApprovalRequest.status != "pending")
        async with self.db.session() as s:
            rows = (await s.execute(query)).all()
        return [
            {
                "approval_id": approval.id,
                "session_id": approval.session_id,
                "requested_by": (user_data or {}).get("name") or user_id,
                "user_id": user_id,
                "status": approval.status,
                "decided_by": approval.decided_by,
                "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
                "submitted_reference": reference,
                "claim_draft": {
                    "policy_number": draft.policy_number,
                    "claim_amount": draft.claim_amount,
                    "documents": draft.documents,
                },
                "probability_score": draft.probability,
                "created_at": approval.created_at.isoformat(),
            }
            for approval, draft, user_id, user_data, reference in rows
        ]

    async def list_pending(self) -> list[dict]:
        return await self.list_approvals("pending")

    async def decide(
        self, approval_id: int, *, decision: t.Literal["approved", "rejected"], decided_by: str, reason: str | None = None
    ) -> dict:
        async with self.db.session() as s:
            approval = await s.get(ApprovalRequest, approval_id)
            if approval is None:
                raise LookupError(f"Approval {approval_id} not found")
            if approval.status != "pending":
                raise ValueError(f"Approval {approval_id} already decided: {approval.status}")

            approval.status = decision
            approval.decided_by = decided_by
            approval.reason = reason
            approval.decided_at = _utcnow()
            draft = await s.get(ClaimDraft, approval.claim_draft_id)
            session_id = approval.session_id
            await s.commit()

        await self._emit(
            session_id,
            "approval_decided",
            {"approval_id": approval_id, "status": decision, "decided_by": decided_by},
        )

        if decision == "approved":
            await self._submit_claim(session_id, approval_id, draft)

        # The caller's session resumes either way; the agent's next turn
        # opens with the outcome.
        await self.store.update_state(session_id, status="active")
        async with self.db.session() as s:
            call_session = await s.get(CallSession, session_id)
            if call_session is not None:
                call_session.status = "active"
                await s.commit()

        return {"approval_id": approval_id, "status": decision}

    async def _submit_claim(self, session_id: str, approval_id: int, draft: ClaimDraft) -> None:
        async def observer(event_type: str, data: dict) -> None:
            await self._emit(session_id, event_type, data)

        try:
            result = await self.registry.invoke(
                "submit_claim",
                {"policy_number": draft.policy_number, "claim_amount": draft.claim_amount, "documents": draft.documents},
                session_id=session_id,
                observer=observer,
            )
        except ToolExhaustedError as err:
            await self._emit(session_id, "claim_submitted", {"claim_id": None, "status": "failed", "error": str(err)})
            return

        async with self.db.session() as s:
            submission = ClaimSubmission(
                session_id=session_id,
                claim_draft_id=draft.id,
                reference=result.output["reference"],
                status="submitted",
            )
            s.add(submission)
            await s.flush()
            claim_id = submission.id
            await s.commit()

        await self._emit(
            session_id,
            "claim_submitted",
            {"claim_id": claim_id, "status": "submitted", "reference": result.output["reference"]},
        )
