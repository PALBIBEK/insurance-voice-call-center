"""All HTTP + WS endpoints.

Routers stay thin: resolve the context, call one service method, shape
the response. The api_key guard covers everything under /api except the
voice webhook (ElevenLabs can't send our header; it authenticates with a
shared secret instead).
"""

import asyncio
import json
import re
import time
import typing as t
import uuid

import httpx
import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect, status
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from insurance_voice.factory import AppContext
from insurance_voice.services.auth_service import AuthError


api_router = APIRouter(prefix="/api")
# Auth endpoints skip the x-api-key guard: a client that hasn't logged
# in yet can't hold credentials.
auth_router = APIRouter(prefix="/api/auth")
# Unauthenticated: probes (docker healthcheck, load balancers) can't send keys.
health_router = APIRouter()
ws_router = APIRouter()


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


CtxDep = t.Annotated[AppContext, Depends(get_ctx)]


async def require_api_key(request: Request, ctx: CtxDep) -> None:
    expected = ctx.settings.API_KEY
    if expected and request.headers.get("x-api-key") != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid X-API-Key")


def bearer_user_id(request: Request, ctx: CtxDep) -> str | None:
    """Logged-in user id from `Authorization: Bearer <token>`, or None.
    Invalid/expired tokens are a hard 401 - silently downgrading a caller
    who thinks they're logged in would be worse than rejecting them."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    try:
        return ctx.auth.verify_token(header[7:])
    except AuthError as err:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(err))


BearerUserDep = t.Annotated[str | None, Depends(bearer_user_id)]


# ---------------------------------------------------------------- health


@health_router.get("/health")
async def health(ctx: CtxDep) -> JSONResponse:
    """Readiness probe: proves the API can reach its state stores, not just
    that uvicorn accepted the socket. 503 with the failing dependency named
    so `docker compose ps` / a reviewer's curl points straight at the cause."""
    checks = {"database": "ok", "session_store": "ok"}
    try:
        async with ctx.db.session() as s:
            await s.execute(sa.text("SELECT 1"))
    except Exception as exc:
        checks["database"] = f"error: {exc}"
    try:
        await ctx.store.ping()
    except Exception as exc:
        checks["session_store"] = f"error: {exc}"

    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "ok" if healthy else "degraded", "checks": checks},
    )


# ---------------------------------------------------------------- auth


class LoginBody(BaseModel):
    user_id: str
    password: str


@auth_router.post("/login")
async def login(body: LoginBody, ctx: CtxDep) -> dict:
    try:
        return await ctx.auth.login(body.user_id, body.password)
    except AuthError as err:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(err))


# ---------------------------------------------------------------- sessions


class CreateSessionBody(BaseModel):
    channel: t.Literal["voice", "text"] = "text"
    user_id: str = "anonymous"


@api_router.post("/sessions", status_code=201, dependencies=[Depends(require_api_key)])
async def create_session(body: CreateSessionBody, ctx: CtxDep, bearer_user: BearerUserDep) -> dict:
    # A verified login outranks whatever user_id the client body claims.
    return await ctx.sessions.create_session(body.channel, user_id=bearer_user or body.user_id)


@api_router.get("/sessions", dependencies=[Depends(require_api_key)])
async def list_sessions(ctx: CtxDep, user_id: str = "anonymous", limit: int = 50) -> list[dict]:
    """The user's conversation history, newest first (sidebar data source)."""
    return await ctx.sessions.list_sessions(user_id, limit=min(limit, 200))


@api_router.get("/sessions/{session_id}", dependencies=[Depends(require_api_key)])
async def get_session(session_id: str, ctx: CtxDep) -> dict:
    return await ctx.sessions.get_session(session_id)


@api_router.post("/sessions/{session_id}/end", dependencies=[Depends(require_api_key)])
async def end_session(session_id: str, ctx: CtxDep) -> dict:
    return await ctx.sessions.end_session(session_id)


@api_router.get("/sessions/{session_id}/turns", dependencies=[Depends(require_api_key)])
async def list_turns(session_id: str, ctx: CtxDep) -> list[dict]:
    return await ctx.sessions.list_turns(session_id)


@api_router.get("/sessions/{session_id}/trace", dependencies=[Depends(require_api_key)])
async def get_trace(session_id: str, ctx: CtxDep) -> list[dict]:
    return await ctx.sessions.get_trace(session_id)


@api_router.get("/sessions/{session_id}/agent-log", dependencies=[Depends(require_api_key)])
async def get_agent_log(session_id: str, ctx: CtxDep) -> list[dict]:
    """Ordered step-by-step reconstruction of everything the system did in a session."""
    return await ctx.sessions.list_agent_log(session_id)


@api_router.get("/metrics/tools", dependencies=[Depends(require_api_key)])
async def tool_metrics(ctx: CtxDep) -> list[dict]:
    """Per-tool analytics from agent_turn_log: attempt counts, success rate, average latency."""
    return await ctx.sessions.tool_metrics()


# ---------------------------------------------------------------- approvals


class DecisionBody(BaseModel):
    decision: t.Literal["approved", "rejected"]
    decided_by: str = "reviewer"
    reason: str | None = None


@api_router.get("/approvals", dependencies=[Depends(require_api_key)])
async def list_approvals(ctx: CtxDep, status: str = "pending") -> list[dict]:  # noqa: A002 - matches query param name
    if status not in ("pending", "decided", "all"):
        raise HTTPException(status_code=422, detail="status must be pending, decided or all")
    return await ctx.approvals.list_approvals(status)


@api_router.post("/approvals/{approval_id}/decision", dependencies=[Depends(require_api_key)])
async def decide_approval(approval_id: int, body: DecisionBody, ctx: CtxDep) -> dict:
    return await ctx.approvals.decide(
        approval_id, decision=body.decision, decided_by=body.decided_by, reason=body.reason
    )


# ---------------------------------------------------------------- voice STT/TTS (ElevenLabs proxy)

# The browser never sees the ElevenLabs key: the frontend sends audio/text
# here and the backend calls ElevenLabs outbound. This is the local-testing
# voice path; the Conversational-AI custom-LLM webhook below is the
# production path (needs a publicly reachable URL).

ELEVENLABS_BASE = "https://api.elevenlabs.io/v1"
TTS_MAX_CHARS = 400  # protect the small credit budget from runaway replies


def _require_elevenlabs(ctx: AppContext) -> str:
    if not ctx.settings.ELEVENLABS_API_KEY:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ElevenLabs API key not configured")
    return ctx.settings.ELEVENLABS_API_KEY


@api_router.post("/voice/stt", dependencies=[Depends(require_api_key)])
async def voice_stt(request: Request, ctx: CtxDep) -> dict:
    """Browser mic audio -> text (ElevenLabs scribe)."""
    api_key = _require_elevenlabs(ctx)
    audio = await request.body()
    if not audio:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Empty audio body")
    content_type = request.headers.get("content-type", "audio/webm")
    async with httpx.AsyncClient(timeout=60) as client:
        res = await client.post(
            f"{ELEVENLABS_BASE}/speech-to-text",
            headers={"xi-api-key": api_key},
            files={"file": ("mic-audio", audio, content_type)},
            data={"model_id": "scribe_v1", "language_code": "eng"},
        )
    if res.status_code != 200:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"ElevenLabs STT: {res.text[:200]}")
    return {"text": (res.json().get("text") or "").strip()}


class TTSBody(BaseModel):
    text: str


@api_router.post("/voice/tts", dependencies=[Depends(require_api_key)])
async def voice_tts(body: TTSBody, ctx: CtxDep) -> Response:
    """Agent reply text -> spoken audio (ElevenLabs TTS, mp3)."""
    api_key = _require_elevenlabs(ctx)
    text = body.text.strip()[:TTS_MAX_CHARS]
    if not text:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Empty text")
    async with httpx.AsyncClient(timeout=60) as client:
        res = await client.post(
            f"{ELEVENLABS_BASE}/text-to-speech/{ctx.settings.ELEVENLABS_VOICE_ID}",
            params={"output_format": "mp3_44100_64"},
            headers={"xi-api-key": api_key},
            json={"text": text, "model_id": ctx.settings.ELEVENLABS_TTS_MODEL},
        )
    if res.status_code != 200:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"ElevenLabs TTS: {res.text[:200]}")
    return Response(content=res.content, media_type="audio/mpeg")


# ---------------------------------------------------------------- voice webhook (ElevenLabs custom LLM)

# Spoken while a slow tool call is in flight - ends with a pause-friendly
# period so TTS phrases it naturally before the real answer follows.
FILLER_PHRASE = "One moment while I check that for you. "


def _sse_chunk(completion_id: str, model: str, delta: dict, finish_reason: str | None = None) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}\n\n"


@api_router.post("/voice/completions")
async def voice_completions(request: Request, ctx: CtxDep) -> t.Any:
    secret = ctx.settings.VOICE_WEBHOOK_SECRET
    if secret and request.query_params.get("secret") != secret:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook secret")

    body = await request.json()
    messages = body.get("messages", [])
    user_text = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")

    session_id = (
        (body.get("metadata") or {}).get("session_id")
        or request.headers.get("x-session-id")
        or ""
    )
    if not session_id:
        # ElevenLabs won't create our session first - do it lazily per call
        created = await ctx.sessions.create_session("voice")
        session_id = created["session_id"]

    model = body.get("model", "insurance-voice")
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    if not body.get("stream", False):
        reply = await ctx.gateway.handle_turn(session_id, user_text)
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}
            ],
        }

    async def stream() -> t.AsyncIterator[str]:
        """The stream opens BEFORE the turn runs. While the agent thinks /
        tools sleep their 2-3s, we watch the session's event feed and speak
        a short filler on the first tool call, so the caller never sits in
        dead air. The real reply then streams word-by-word."""
        yield _sse_chunk(completion_id, model, {"role": "assistant"})

        filler_sent = False
        async with ctx.bus.subscribe(session_id) as events:
            turn_task = ctx.spawn_turn(ctx.gateway.handle_turn(session_id, user_text))
            while not turn_task.done():
                event_getter = asyncio.ensure_future(events.get())
                done, _pending = await asyncio.wait(
                    {turn_task, event_getter}, return_when=asyncio.FIRST_COMPLETED
                )
                if event_getter in done:
                    event = event_getter.result()
                    if event.get("type") == "tool_call_started" and not filler_sent:
                        filler_sent = True
                        yield _sse_chunk(completion_id, model, {"content": FILLER_PHRASE})
                else:
                    event_getter.cancel()

        try:
            reply = turn_task.result()
        except Exception:  # keep the voice channel alive no matter what
            reply = "I'm sorry, I hit a snag with that. Could you say it once more?"

        # Word-level pseudo-streaming: ElevenLabs starts speaking on the
        # first chunk instead of waiting for the full reply.
        for token in re.findall(r"\S+\s*", reply):
            yield _sse_chunk(completion_id, model, {"content": token})
        yield _sse_chunk(completion_id, model, {}, finish_reason="stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# ---------------------------------------------------------------- frontend realtime channel


@ws_router.websocket("/ws/sessions/{session_id}")
async def session_ws(websocket: WebSocket, session_id: str) -> None:
    ctx: AppContext = websocket.app.state.ctx
    state = await ctx.store.get_state(session_id)
    if not state:
        # Hot store is a cache - a reopened conversation after a backend
        # restart rebuilds its state from the durable rows.
        state = await ctx.gateway.rehydrate_state(session_id)
    if not state:
        await websocket.close(code=4404, reason="unknown session")
        return

    await websocket.accept()
    await websocket.send_json(
        {
            "type": "connection_ack",
            "session_id": session_id,
            "data": {"current_agent": state.get("current_agent", "triage"), "status": state.get("status", "active")},
        }
    )

    async def run_turn_reporting_errors(content: str) -> None:
        try:
            await ctx.gateway.handle_turn(session_id, content)
        except Exception as exc:  # a dead-silent turn is worse than any error
            await ctx.bus.publish(
                session_id,
                {"type": "error", "session_id": session_id, "data": {"code": "turn_failed", "message": str(exc)}},
            )

    async with ctx.bus.subscribe(session_id) as queue:

        async def relay() -> None:
            while True:
                event = await queue.get()
                await websocket.send_json(event)

        relay_task = asyncio.create_task(relay())
        try:
            while True:
                message = await websocket.receive_json()
                if message.get("type") == "user_message":
                    content = (message.get("data") or {}).get("content", "").strip()
                    if content:
                        # Turns are owned by the app context, not this handler:
                        # a disconnect cancels this handler task, and anything
                        # awaited here (even a gather over the turns) would be
                        # cancelled with it - aborting business logic mid-write.
                        # The relay keeps streaming events while turns run.
                        ctx.spawn_turn(run_turn_reporting_errors(content))
        except WebSocketDisconnect:
            pass
        finally:
            relay_task.cancel()
