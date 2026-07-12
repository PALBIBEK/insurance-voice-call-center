"""HITL approval flow: request -> pending -> human decision -> gated submit.

The real submit_claim tool only ever fires from ApprovalService.decide()
on approval - never from conversation. Both decision paths are covered.
"""

import uuid

import pytest
import sqlalchemy as sa

from insurance_voice.db.models import ApprovalRequest, CallSession, ClaimSubmission
from insurance_voice.db.session import Database
from insurance_voice.services.approval_service import ApprovalService
from insurance_voice.services.event_bus import InMemoryEventBus
from insurance_voice.services.session_store import InMemorySessionStore
from insurance_voice.services.tools import ToolPolicy, build_default_registry


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


@pytest.fixture
def service(db):
    policy = ToolPolicy(latency_min_s=0.01, latency_max_s=0.02, failure_rate=0.0, max_retries=2, retry_backoff_base_s=0.01)
    return ApprovalService(
        db=db,
        registry=build_default_registry(policy),
        store=InMemorySessionStore(),
        bus=InMemoryEventBus(),
    )


async def seed_session(db) -> str:
    session_id = str(uuid.uuid4())
    async with db.session() as s:
        s.add(CallSession(id=session_id, channel="text"))
        await s.commit()
    return session_id


CLAIM_ARGS = {"policy_number": "POL-1001", "claim_amount": 50000, "documents": ["discharge_summary", "bills", "id_proof"]}


async def test_request_approval_creates_pending_row_and_event(db, service):
    session_id = await seed_session(db)
    async with service.bus.subscribe(session_id) as queue:
        approval_id = await service.request_approval(session_id, CLAIM_ARGS)
        event = queue.get_nowait()

    assert event["type"] == "approval_required"
    assert event["data"]["approval_id"] == approval_id
    assert event["data"]["claim_draft"]["policy_number"] == "POL-1001"

    async with db.session() as s:
        approval = await s.get(ApprovalRequest, approval_id)
        assert approval.status == "pending"

    pending = await service.list_pending()
    assert [p["approval_id"] for p in pending] == [approval_id]


async def test_approve_submits_claim_and_reactivates_session(db, service):
    session_id = await seed_session(db)
    await service.store.set_state(session_id, {"status": "pending_approval", "current_agent": "claims"})
    approval_id = await service.request_approval(session_id, CLAIM_ARGS)

    async with service.bus.subscribe(session_id) as queue:
        outcome = await service.decide(approval_id, decision="approved", decided_by="reviewer@test")
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())

    assert outcome["status"] == "approved"
    types = [e["type"] for e in events]
    assert "approval_decided" in types
    assert "tool_call_started" in types  # the real submit_claim ran, through the normal tool wrapper
    assert "claim_submitted" in types
    submitted = next(e for e in events if e["type"] == "claim_submitted")
    assert submitted["data"]["status"] == "submitted"
    assert submitted["data"]["reference"].startswith("CLM-")

    async with db.session() as s:
        submission = (await s.scalars(sa.select(ClaimSubmission))).one()
        assert submission.session_id == session_id
        approval = await s.get(ApprovalRequest, approval_id)
        assert approval.status == "approved"
        assert approval.decided_by == "reviewer@test"
        assert approval.decided_at is not None

    assert (await service.store.get_state(session_id))["status"] == "active"


async def test_reject_never_calls_submit_claim(db, service):
    session_id = await seed_session(db)
    await service.store.set_state(session_id, {"status": "pending_approval", "current_agent": "claims"})
    approval_id = await service.request_approval(session_id, CLAIM_ARGS)

    async with service.bus.subscribe(session_id) as queue:
        outcome = await service.decide(approval_id, decision="rejected", decided_by="reviewer@test", reason="incomplete docs")
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())

    assert outcome["status"] == "rejected"
    types = [e["type"] for e in events]
    assert "approval_decided" in types
    assert "tool_call_started" not in types
    assert "claim_submitted" not in types

    async with db.session() as s:
        assert (await s.scalars(sa.select(ClaimSubmission))).all() == []

    assert (await service.store.get_state(session_id))["status"] == "active"


async def test_approved_submit_that_500s_to_exhaustion_fails_loudly_not_silently(db, service):
    """submit_claim 500s to exhaustion AFTER a human approved: retries run,
    the caller gets a claim_submitted status=failed event (not silence,
    not a crash), no submission row is written, and the session
    reactivates so the agent can address it."""
    session_id = await seed_session(db)
    await service.store.set_state(session_id, {"status": "pending_approval", "current_agent": "claims"})
    approval_id = await service.request_approval(session_id, CLAIM_ARGS)
    service.registry.force_failures("submit_claim", 2)  # == max_retries: every attempt 500s

    async with service.bus.subscribe(session_id) as queue:
        outcome = await service.decide(approval_id, decision="approved", decided_by="reviewer@test")
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())

    assert outcome["status"] == "approved"
    types = [e["type"] for e in events]
    assert types.count("tool_call_failed") == 2  # both attempts visible in the trace
    assert "tool_exhausted" in types
    submitted = next(e for e in events if e["type"] == "claim_submitted")
    assert submitted["data"]["status"] == "failed"
    assert submitted["data"]["claim_id"] is None

    async with db.session() as s:
        assert (await s.scalars(sa.select(ClaimSubmission))).all() == []
        approval = await s.get(ApprovalRequest, approval_id)
        assert approval.status == "approved"  # the human's decision stands; only the submission failed

    assert (await service.store.get_state(session_id))["status"] == "active"


async def test_double_decision_is_rejected(db, service):
    session_id = await seed_session(db)
    approval_id = await service.request_approval(session_id, CLAIM_ARGS)
    await service.decide(approval_id, decision="approved", decided_by="a")
    with pytest.raises(ValueError):
        await service.decide(approval_id, decision="rejected", decided_by="b")
