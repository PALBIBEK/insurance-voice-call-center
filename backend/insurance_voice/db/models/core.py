"""All persistent entities.

Postgres (or SQLite in dev/tests) is the durable system of record;
Redis holds only the hot per-session coordination state. Every state
transition that matters for auditing is its own row here (handoffs,
per-attempt tool invocations, approval decisions), never inferred.
"""

import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from insurance_voice.db.base import Base, TimestampMixin


class UserInfo(Base, TimestampMixin):
    __tablename__ = "user_info"

    user_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    # sha256 hex digest - demo-grade; a production system would use bcrypt/argon2
    password_hash: Mapped[str] = mapped_column(sa.String(64))
    # Profile handed to the agents on login (name, policy_number, city, ...)
    user_data: Mapped[dict] = mapped_column(default=dict)


class CallSession(Base, TimestampMixin):
    __tablename__ = "call_session"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True)  # uuid4
    # Caller identity: a conversation is uniquely identified by (user_id, id).
    # No auth system in scope, so this is a client-supplied stable identifier.
    user_id: Mapped[str] = mapped_column(sa.String(64), default="anonymous", index=True)
    status: Mapped[str] = mapped_column(sa.String(20), default="active")  # active|pending_approval|completed|abandoned
    current_agent: Mapped[str] = mapped_column(sa.String(20), default="triage")  # triage|policy|claims
    channel: Mapped[str] = mapped_column(sa.String(10))  # voice|text
    policy_number: Mapped[str | None] = mapped_column(sa.String(20), default=None)
    ended_at: Mapped[datetime.datetime | None] = mapped_column(sa.DateTime(timezone=True), default=None)


class Turn(Base, TimestampMixin):
    __tablename__ = "turn"

    id: Mapped[int] = mapped_column(sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True)
    session_id: Mapped[str] = mapped_column(sa.ForeignKey("call_session.id"), index=True)
    role: Mapped[str] = mapped_column(sa.String(10))  # user|agent|system
    agent_name: Mapped[str | None] = mapped_column(sa.String(20), default=None)
    content: Mapped[str] = mapped_column(sa.Text)


class AgentHandoff(Base, TimestampMixin):
    __tablename__ = "agent_handoff"

    id: Mapped[int] = mapped_column(sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True)
    session_id: Mapped[str] = mapped_column(sa.ForeignKey("call_session.id"), index=True)
    from_agent: Mapped[str] = mapped_column(sa.String(20))
    to_agent: Mapped[str] = mapped_column(sa.String(20))
    reason: Mapped[str] = mapped_column(sa.Text, default="")


class ToolInvocation(Base, TimestampMixin):
    __tablename__ = "tool_invocation"

    id: Mapped[int] = mapped_column(sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True)
    session_id: Mapped[str] = mapped_column(sa.ForeignKey("call_session.id"), index=True)
    tool_name: Mapped[str] = mapped_column(sa.String(50))
    arguments: Mapped[dict] = mapped_column(default=dict)
    attempt_number: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(sa.String(12), default="pending")  # pending|succeeded|failed|retrying
    latency_ms: Mapped[int | None] = mapped_column(default=None)
    error_message: Mapped[str | None] = mapped_column(sa.Text, default=None)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(sa.DateTime(timezone=True), default=None)


class ClaimDraft(Base, TimestampMixin):
    __tablename__ = "claim_draft"

    id: Mapped[int] = mapped_column(sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True)
    session_id: Mapped[str] = mapped_column(sa.ForeignKey("call_session.id"), index=True)
    policy_number: Mapped[str] = mapped_column(sa.String(20))
    claim_amount: Mapped[float] = mapped_column(default=0.0)
    documents: Mapped[list] = mapped_column(default=list)
    probability: Mapped[float | None] = mapped_column(default=None)


class ClaimSubmission(Base, TimestampMixin):
    __tablename__ = "claim_submission"

    id: Mapped[int] = mapped_column(sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True)
    session_id: Mapped[str] = mapped_column(sa.ForeignKey("call_session.id"), index=True)
    claim_draft_id: Mapped[int] = mapped_column(sa.ForeignKey("claim_draft.id"))
    reference: Mapped[str] = mapped_column(sa.String(20))
    status: Mapped[str] = mapped_column(sa.String(12), default="submitted")  # submitted|failed


class ApprovalRequest(Base, TimestampMixin):
    __tablename__ = "approval_request"

    id: Mapped[int] = mapped_column(sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True)
    session_id: Mapped[str] = mapped_column(sa.ForeignKey("call_session.id"), index=True)
    claim_draft_id: Mapped[int] = mapped_column(sa.ForeignKey("claim_draft.id"))
    status: Mapped[str] = mapped_column(sa.String(10), default="pending")  # pending|approved|rejected
    decided_by: Mapped[str | None] = mapped_column(sa.String(100), default=None)
    reason: Mapped[str | None] = mapped_column(sa.Text, default=None)
    decided_at: Mapped[datetime.datetime | None] = mapped_column(sa.DateTime(timezone=True), default=None)


class AgentTurnLog(Base, TimestampMixin):
    """Flat, ordered log of every observable step the system takes in a
    session - one row per step, sparse columns by step_type. ORDER BY
    turn_step replays the whole execution; GROUP BY tool_name answers
    latency/success-rate analytics without joins."""

    __tablename__ = "agent_turn_log"
    __table_args__ = (sa.UniqueConstraint("session_id", "turn_step"),)

    id: Mapped[int] = mapped_column(sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True)
    session_id: Mapped[str] = mapped_column(sa.ForeignKey("call_session.id"), index=True)
    user_id: Mapped[str] = mapped_column(sa.String(64), index=True)  # denormalized for per-user analytics
    turn_step: Mapped[int] = mapped_column()  # 1,2,3... monotonic within a session
    step_type: Mapped[str] = mapped_column(sa.String(20))  # user_message|agent_message|tool_call|handoff|approval_requested|approval_decided
    agent_name: Mapped[str | None] = mapped_column(sa.String(20), default=None)
    message: Mapped[str | None] = mapped_column(sa.Text, default=None)
    tool_name: Mapped[str | None] = mapped_column(sa.String(50), default=None)
    tool_args: Mapped[dict | None] = mapped_column(default=None)
    tool_status: Mapped[str | None] = mapped_column(sa.String(12), default=None)  # succeeded|failed|exhausted
    latency_ms: Mapped[int | None] = mapped_column(default=None)


class TraceSpan(Base, TimestampMixin):
    __tablename__ = "trace_span"

    id: Mapped[int] = mapped_column(sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True)
    session_id: Mapped[str] = mapped_column(sa.ForeignKey("call_session.id"), index=True)
    span_id: Mapped[str] = mapped_column(sa.String(36))
    parent_span_id: Mapped[str | None] = mapped_column(sa.String(36), default=None)
    kind: Mapped[str] = mapped_column(sa.String(20))  # agent_run|tool_call|handoff|guardrail
    payload: Mapped[dict] = mapped_column(default=dict)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(sa.DateTime(timezone=True), default=None)
