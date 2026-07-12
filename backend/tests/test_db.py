"""DB layer: async engine/session lifecycle and the core table roundtrips."""

import uuid

import pytest
import sqlalchemy as sa

from insurance_voice.db.session import Database


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


async def test_call_session_roundtrip(db):
    from insurance_voice.db.models import CallSession

    session_id = str(uuid.uuid4())
    async with db.session() as s:
        s.add(CallSession(id=session_id, channel="text"))
        await s.commit()

    async with db.session() as s:
        row = await s.get(CallSession, session_id)
        assert row is not None
        assert row.status == "active"
        assert row.current_agent == "triage"
        assert row.created_at is not None


async def test_turn_and_handoff_rows(db):
    from insurance_voice.db.models import AgentHandoff, CallSession, Turn

    session_id = str(uuid.uuid4())
    async with db.session() as s:
        s.add(CallSession(id=session_id, channel="voice"))
        s.add(Turn(session_id=session_id, role="user", content="I want to file a claim"))
        s.add(Turn(session_id=session_id, role="agent", agent_name="triage", content="Routing you..."))
        s.add(AgentHandoff(session_id=session_id, from_agent="triage", to_agent="claims", reason="claim intent"))
        await s.commit()

    async with db.session() as s:
        turns = (await s.scalars(sa.select(Turn).where(Turn.session_id == session_id).order_by(Turn.id))).all()
        assert [t.role for t in turns] == ["user", "agent"]
        handoff = (await s.scalars(sa.select(AgentHandoff).where(AgentHandoff.session_id == session_id))).one()
        assert (handoff.from_agent, handoff.to_agent) == ("triage", "claims")


async def test_tool_invocation_one_row_per_attempt(db):
    from insurance_voice.db.models import CallSession, ToolInvocation

    session_id = str(uuid.uuid4())
    async with db.session() as s:
        s.add(CallSession(id=session_id, channel="text"))
        s.add(ToolInvocation(session_id=session_id, tool_name="submit_claim", arguments={"a": 1},
                             attempt_number=1, status="failed", error_message="500"))
        s.add(ToolInvocation(session_id=session_id, tool_name="submit_claim", arguments={"a": 1},
                             attempt_number=2, status="succeeded", latency_ms=2400))
        await s.commit()

    async with db.session() as s:
        rows = (await s.scalars(
            sa.select(ToolInvocation).where(ToolInvocation.session_id == session_id).order_by(ToolInvocation.attempt_number)
        )).all()
        assert len(rows) == 2
        assert rows[0].status == "failed" and rows[1].status == "succeeded"
        assert rows[1].arguments == {"a": 1}


async def test_claim_and_approval_lifecycle(db):
    from insurance_voice.db.models import ApprovalRequest, CallSession, ClaimDraft

    session_id = str(uuid.uuid4())
    async with db.session() as s:
        s.add(CallSession(id=session_id, channel="text"))
        draft = ClaimDraft(session_id=session_id, policy_number="POL-1001", claim_amount=50000,
                           documents=["bills"], probability=0.72)
        s.add(draft)
        await s.flush()
        s.add(ApprovalRequest(session_id=session_id, claim_draft_id=draft.id))
        await s.commit()

    async with db.session() as s:
        approval = (await s.scalars(sa.select(ApprovalRequest))).one()
        assert approval.status == "pending"
        assert approval.decided_at is None


async def test_trace_span_tree(db):
    from insurance_voice.db.models import CallSession, TraceSpan

    session_id = str(uuid.uuid4())
    async with db.session() as s:
        s.add(CallSession(id=session_id, channel="text"))
        s.add(TraceSpan(session_id=session_id, span_id="a", kind="agent_run", payload={"agent": "claims"}))
        s.add(TraceSpan(session_id=session_id, span_id="b", parent_span_id="a", kind="tool_call",
                        payload={"tool": "submit_claim"}))
        await s.commit()

    async with db.session() as s:
        spans = (await s.scalars(sa.select(TraceSpan).where(TraceSpan.session_id == session_id))).all()
        by_id = {sp.span_id: sp for sp in spans}
        assert by_id["b"].parent_span_id == "a"
