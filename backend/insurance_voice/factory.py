"""FastAPI app factory: builds and wires every layer once, exposes app.state.ctx.

Chat client resolution order: injected (tests) -> AsyncOpenAI against
OpenRouter (real key present) -> rule-based demo client (no key, offline
demo). Only this seam changes across those three modes.
"""

import asyncio
import contextlib
import dataclasses
import pathlib
import typing as t

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from insurance_voice.config.settings import Settings
import sqlalchemy as sa

from insurance_voice.db.models import ApprovalRequest, CallSession, ClaimDraft, ClaimSubmission, UserInfo
from insurance_voice.db.session import Database
from insurance_voice.services.approval_service import ApprovalService
from insurance_voice.services.auth_service import AuthService
from insurance_voice.services.agents.runtime import AgentRuntime
from insurance_voice.services.drift_service import DriftService
from insurance_voice.services.event_bus import EventBus, build_event_bus
from insurance_voice.services.gateway import GatewayService
from insurance_voice.services.session_service import SessionNotFoundError, SessionService
from insurance_voice.services.session_store import InMemorySessionStore, SessionStore, build_session_store
from insurance_voice.services.tools import ToolPolicy, ToolRegistry, build_default_registry
from insurance_voice.services.trace_recorder import RecordingEventBus


@dataclasses.dataclass
class AppContext:
    settings: Settings
    db: Database
    store: SessionStore
    bus: EventBus
    registry: ToolRegistry
    runtime: AgentRuntime
    gateway: GatewayService
    sessions: SessionService
    approvals: ApprovalService
    drift: DriftService
    auth: AuthService
    # In-flight turn tasks. Owned here - NOT by the WS handler - so a client
    # disconnect can never cancel a turn mid-write; the lifespan awaits them
    # on shutdown instead.
    inflight_turns: set["asyncio.Task"] = dataclasses.field(default_factory=set)

    def spawn_turn(self, coro: t.Coroutine) -> "asyncio.Task":
        task = asyncio.create_task(coro)
        self.inflight_turns.add(task)
        task.add_done_callback(self.inflight_turns.discard)
        return task

    async def drain_inflight_turns(self, timeout: float = 30.0) -> None:
        if self.inflight_turns:
            await asyncio.wait(self.inflight_turns, timeout=timeout)

    def force_agent_for_tests(self, session_id: str, agent_name: str) -> None:
        """Test-only: jump a session to a specific agent without playing
        through triage. Sync on purpose - called from TestClient's thread."""
        if isinstance(self.store, InMemorySessionStore):
            self.store._state.setdefault(session_id, {})["current_agent"] = agent_name
        else:  # pragma: no cover - not used against Redis
            raise NotImplementedError("force_agent_for_tests only supports the in-memory store")


def _resolve_chat_client(settings: Settings, chat_client: t.Any | None) -> t.Any:
    if chat_client is not None:
        return chat_client
    if settings.OPENROUTER_API_KEY:
        from openai import AsyncOpenAI

        return AsyncOpenAI(base_url=settings.OPENROUTER_BASE_URL, api_key=settings.OPENROUTER_API_KEY)
    from insurance_voice.services.agents.demo_client import DemoChatClient

    return DemoChatClient()


def build_context(settings: Settings, chat_client: t.Any | None = None) -> AppContext:
    db = Database(settings.DATABASE_URL)
    store = build_session_store(settings.REDIS_URL)
    bus = RecordingEventBus(build_event_bus(settings.REDIS_URL), db)

    async def profile_lookup(session_id: str) -> dict | None:
        """session -> its user -> profile; None when the caller never logged in."""
        async with db.session() as s:
            call = await s.get(CallSession, session_id)
            if call is None:
                return None
            user = await s.get(UserInfo, call.user_id)
            return user.user_data if user is not None else None

    async def claims_lookup(session_id: str) -> list[dict]:
        """Every claim the calling session's USER has filed - across all
        their conversations, joined live from approval/draft/submission
        rows. This is how 'what about my claim?' works in a reopened or
        brand-new conversation."""
        async with db.session() as s:
            call = await s.get(CallSession, session_id)
            if call is None:
                return []
            rows = (
                await s.execute(
                    sa.select(ApprovalRequest, ClaimDraft)
                    .join(ClaimDraft, ApprovalRequest.claim_draft_id == ClaimDraft.id)
                    .join(CallSession, ApprovalRequest.session_id == CallSession.id)
                    .where(CallSession.user_id == call.user_id)
                    .order_by(ApprovalRequest.created_at.desc())
                )
            ).all()
            references = {}
            if rows:
                submissions = (
                    await s.scalars(
                        sa.select(ClaimSubmission).where(
                            ClaimSubmission.claim_draft_id.in_([draft.id for _, draft in rows])
                        )
                    )
                ).all()
                references = {sub.claim_draft_id: sub.reference for sub in submissions}
        return [
            {
                "policy_number": draft.policy_number,
                "claim_amount": draft.claim_amount,
                "status": approval.status,  # pending|approved|rejected
                "submitted_reference": references.get(draft.id),
                "decided_by": approval.decided_by,
                "filed_at": approval.created_at.isoformat(),
                "from_this_conversation": approval.session_id == session_id,
            }
            for approval, draft in rows
        ]

    registry = build_default_registry(
        ToolPolicy(
            latency_min_s=settings.TOOL_LATENCY_MIN_S,
            latency_max_s=settings.TOOL_LATENCY_MAX_S,
            failure_rate=settings.TOOL_FAILURE_RATE,
            max_retries=settings.TOOL_MAX_RETRIES,
            retry_backoff_base_s=settings.TOOL_RETRY_BACKOFF_BASE_S,
        ),
        profile_lookup=profile_lookup,
        claims_lookup=claims_lookup,
    )
    approvals = ApprovalService(db=db, registry=registry, store=store, bus=bus)
    resolved_chat = _resolve_chat_client(settings, chat_client)
    runtime = AgentRuntime(
        chat_client=resolved_chat,
        registry=registry,
        store=store,
        bus=bus,
        model_triage=settings.OPENROUTER_MODEL_TRIAGE,
        model_specialist=settings.OPENROUTER_MODEL_SPECIALIST,
        approval_hook=approvals.request_approval,
    )
    drift = DriftService(
        store=store,
        bus=bus,
        max_offdomain_turns=settings.DRIFT_MAX_OFFDOMAIN_TURNS,
        max_stagnant_turns=settings.DRIFT_MAX_STAGNANT_TURNS,
        # Prompt-based topic gate on the cheap triage-tier model. Only wired
        # when the resolved client is the real AsyncOpenAI: injected test
        # doubles and the zero-key demo brain must use the keyword fallback
        # inside DriftService (a scripted fake here would eat responses that
        # belong to the agents).
        chat_client=resolved_chat if (chat_client is None and settings.OPENROUTER_API_KEY) else None,
        model=settings.OPENROUTER_MODEL_TRIAGE,
    )
    gateway = GatewayService(db=db, store=store, bus=bus, runtime=runtime, drift=drift)
    sessions = SessionService(db=db, store=store, bus=bus)
    auth = AuthService(db=db, secret_key=settings.SECRET_KEY)
    return AppContext(
        settings=settings,
        db=db,
        store=store,
        bus=bus,
        registry=registry,
        runtime=runtime,
        gateway=gateway,
        sessions=sessions,
        approvals=approvals,
        drift=drift,
        auth=auth,
    )


def create_app(settings: Settings | None = None, chat_client: t.Any | None = None) -> FastAPI:
    from insurance_voice.web.routes import api_router, auth_router, health_router, ws_router

    if settings is None:
        from insurance_voice.config import get_settings

        settings = get_settings()

    ctx = build_context(settings, chat_client)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        await ctx.db.create_all()
        await ctx.auth.seed_demo_user(settings.DEMO_USER_ID, settings.DEMO_USER_PASSWORD)
        yield
        # Let in-flight turns finish before tearing the DB down - cancelling
        # a turn mid-write is never acceptable, including at shutdown.
        await ctx.drain_inflight_turns()
        await ctx.db.dispose()

    app = FastAPI(
        title="Insurance Voice Call Center",
        lifespan=lifespan,
        docs_url="/docs" if settings.ENV == "development" else None,
    )
    app.state.ctx = ctx
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(api_router)
    app.include_router(ws_router)

    # The UI is a separate client served by its own process (frontend/, port 3000).
    # CORS lets that separate origin call this API from the browser.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Convenience mode only: if the sibling frontend/ checkout is present on disk,
    # serve its index at "/" so single-process demos still work. The canonical
    # setup runs the UI as its own process — see frontend/serve.py.
    frontend_dir = pathlib.Path(__file__).resolve().parents[2] / "frontend"
    frontend_file = frontend_dir / "index.html"
    if frontend_file.exists():

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(frontend_file)

        @app.get("/reviewer.html", include_in_schema=False)
        async def reviewer() -> FileResponse:
            return FileResponse(frontend_dir / "reviewer.html")

    @app.exception_handler(SessionNotFoundError)
    async def _session_not_found(_request: Request, exc: SessionNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": {"code": "session_not_found", "message": str(exc)}})

    @app.exception_handler(LookupError)
    async def _not_found(_request: Request, exc: LookupError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": {"code": "not_found", "message": str(exc)}})

    @app.exception_handler(ValueError)
    async def _conflict(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": {"code": "conflict", "message": str(exc)}})

    return app
