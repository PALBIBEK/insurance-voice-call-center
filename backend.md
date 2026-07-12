
# Backend Design: Real-Time Autonomous Insurance Call Center

Source: `assignment.txt` + `requirement.md`. This document is the concrete
engineering design for the backend service — folder layout, layer
responsibilities, data model, concurrency model, and the interfaces each
layer exposes to the ones above/below it. It is the thing implementation
should be checked against, not a copy of the requirement doc.

Everything here is written against one hard constraint that shapes almost
every decision below: **this is a live, real-time voice/chat surface, not a
batch system**. Any unit of work that sits between "user spoke" and "agent
responds" has to finish in low hundreds of milliseconds or be made
observably asynchronous (filler speech, a "still working" event) — it can
never be handed off to a separate worker process and polled for later. That
single fact is why the background-execution story here is deliberately
*not* a task-queue/worker-pool architecture, even though that is the
default reach for "background work" in most backend stacks. More on this in
§6.

## 1. Layering & module layout

Five layers, same discipline top to bottom regardless of whether a request
comes in over HTTP, WebSocket, or the ElevenLabs custom-LLM webhook:

```
web/       -> HTTP/WS endpoints. Parse request, call one service method, shape response. No business logic.
schema/    -> Pydantic models for request/response bodies and WS message envelopes. No behavior.
services/  -> Business logic, orchestration, mock tools. This is where the "brain" lives.
db/        -> SQLAlchemy models + async session. Pure persistence, no business rules.
config/    -> Settings, env loading, logging setup. Imported by everything, depends on nothing.
```

Folder tree:

```
insurance_voice/
├── asgi.py                      # uvicorn entrypoint, run_server()
├── factory.py                   # get_app() FastAPI factory (singleton), lifespan
├── __init__.py                  # package-level logger, settings, contextvars
│
├── config/
│   ├── settings.py               # base Settings (pydantic-settings BaseSettings)
│   ├── dev_settings.py            # overrides: ENV=development, verbose logging
│   ├── prod_settings.py           # overrides: ENV=production
│   ├── logging.py                # structlog / stdlib logging setup, JSON formatter
│   └── constants.py               # enums shared across layers: AgentName, ToolName, ApprovalStatus...
│
├── db/
│   ├── session.py                 # async engine + async_sessionmaker, scoped-per-request accessor
│   ├── base.py                    # Base declarative class, TimestampMixin
│   └── models/
│       ├── call_session.py        # CallSession
│       ├── turn.py                 # Turn (one transcript entry)
│       ├── tool_invocation.py      # ToolInvocation (one tool call attempt)
│       ├── handoff.py              # AgentHandoff (state-transition row)
│       ├── claim.py                # ClaimDraft, ClaimSubmission
│       ├── approval.py            # ApprovalRequest
│       └── trace_span.py          # TraceSpan (custom tracing exporter target)
│
├── schema/
│   ├── base.py                     # BaseSchema(BaseModel) - camelCase alias generator, from_attributes=True
│   ├── session_schema.py
│   ├── voice_schema.py              # ElevenLabs custom-LLM request/response contract
│   ├── ws_schema.py                 # discriminated union of all WS event payloads (see request_api_mapping.md)
│   ├── approval_schema.py
│   ├── claim_schema.py
│   └── error_schema.py
│
├── services/
│   ├── session_service.py           # create/get/expire CallSession, Redis-backed state machine
│   ├── voice_gateway_service.py      # bridges ElevenLabs custom-LLM webhook <-> agent runtime
│   ├── event_bus.py                  # per-session pub/sub fan-out to connected frontend WS clients
│   ├── agents/
│   │   ├── runtime.py                 # builds the openai-agents Runner, OpenRouter client config
│   │   ├── triage_agent.py
│   │   ├── policy_agent.py
│   │   ├── claims_agent.py
│   │   └── guardrails.py             # drift-detection input/output guardrails
│   ├── tools/
│   │   ├── base.py                    # tool decorator: latency injection + failure injection + retry
│   │   ├── policy_tools.py            # get_policy_details, get_hospital_network, get_billing_status
│   │   └── claims_tools.py            # calculate_claim_probability, submit_claim
│   ├── approval_service.py           # HITL: create/approve/reject ApprovalRequest, resumes agent
│   ├── trace_service.py              # custom span exporter -> trace_span table + structured logs
│   └── drift_service.py              # loop/off-domain/hallucinated-tool detectors
│
└── web/
    ├── routes.py                     # aggregates all routers under /api
    ├── dependencies/
    │   ├── db.py                     # DBSessionDep annotated dependency
    │   ├── call_session.py           # resolves & validates CallSession from path/header, sets contextvar
    │   └── auth.py                   # single static API-key guard (see §7)
    ├── middlewares/
    │   ├── logging.py                 # one ASGI middleware, handles both http and websocket scopes
    │   └── exception_handler.py       # maps domain exceptions -> ErrorModel JSON
    ├── sessions.py                    # POST/GET /api/sessions
    ├── voice.py                       # POST /api/voice/completions  (ElevenLabs custom-LLM webhook)
    ├── realtime_ws.py                 # WS  /ws/sessions/{id}         (frontend <-> backend live channel)
    ├── approvals.py                   # GET/POST /api/approvals
    └── claims.py                      # GET /api/claims/{id}
```

This mirrors a pattern that scales well in practice: routers never import
SQLAlchemy directly, services never import FastAPI, and schema objects are
the only things that cross the web/service boundary. Keeping that
discipline here — even though the whole service is small — is what makes
the deterministic test suite possible: every service method is callable
and assertable with no HTTP/ASGI layer in the loop at all (§8).

## 2. Config layer

Use `pydantic-settings` rather than hand-rolled env parsing — free
validation, `.env` support, and typed access everywhere via a single
`Settings` singleton built once in `config/settings.py` and imported as
`from insurance_voice.config import settings`.

Layering: a base `Settings` class with sane dev defaults, and
`ENV=production|development|test` swaps a handful of fields (log level,
DB echo, whether the mock-tool failure injection is forced-on for demos).
No dynaconf-style file cascade needed at this scale — one class with
`env_prefix="IVCC_"` is enough and keeps the config surface auditable in
one file.

Key settings:

| Setting | Purpose |
|---|---|
| `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL` | passed as `base_url` override to the OpenAI-compatible client used by the agents SDK |
| `OPENROUTER_MODEL_TRIAGE`, `OPENROUTER_MODEL_SPECIALIST` | cheap/fast model for triage & routine turns vs a stronger model reserved for claim-probability reasoning — explicit two-tier choice to respect the token budget |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_AGENT_ID` | voice product config |
| `DATABASE_URL` | asyncpg DSN |
| `REDIS_URL` | session store + pub/sub |
| `TOOL_LATENCY_MIN_S` / `TOOL_LATENCY_MAX_S` | injected mock-tool latency window (2–3s per the assignment) |
| `TOOL_FAILURE_RATE` | probability a mock tool raises, for fault-injection testing |
| `TOOL_MAX_RETRIES`, `TOOL_RETRY_BACKOFF_BASE_S` | retry policy before escalating to the user |
| `DRIFT_MAX_STAGNANT_TURNS` | consecutive no-progress turns before forced escalation |
| `SECRET_KEY` | signs any session cookies used by the HITL review page |

Everything secret comes from env vars / `.env` (git-ignored), never
hardcoded — this is one place the design deliberately does *not* imitate
larger internal systems, which sometimes bake defaults into
`settings.py` for convenience; a 3-day greenfield service has no excuse to.

## 3. DB layer

Single **async** SQLAlchemy engine (`asyncpg`) — no dual sync/async engine
story is needed here since this is a greenfield service and everything
that touches the DB is already inside an async request/agent-loop context.
One `async_sessionmaker`, one dependency (`DBSessionDep`) that hands each
request/turn a session scoped to that request and closes it in a
`finally`. No ORM-level "current session" global — pass the session
explicitly through service methods. Explicit is better than a scoped
thread-local here because a single process is juggling many concurrent
call sessions at once (see §6); implicit thread/task-local session magic
is exactly the kind of thing that produces cross-session state bugs under
concurrency, which is one of the things this assignment is explicitly
graded on.

Core tables:

- **`call_session`** — one row per call/chat session. `id (uuid pk)`,
  `status` (`active|pending_approval|completed|abandoned`), `current_agent`
  (`triage|policy|claims`), `channel` (`voice|text`), `created_at`,
  `ended_at`, `policy_number` (nullable, filled once known).
- **`turn`** — one row per conversational turn. `session_id fk`, `role`
  (`user|agent|system`), `agent_name`, `content`, `created_at`. This is the
  durable transcript — independent of whatever ElevenLabs/websocket buffer
  holds live audio.
- **`agent_handoff`** — `session_id fk`, `from_agent`, `to_agent`,
  `reason`, `created_at`. A state-transition log, not inferred after the
  fact from `turn.agent_name` changes — logged explicitly at the moment
  the handoff executes (mirrors the requirement that "state transitions"
  be logged clearly, not reconstructed).
- **`tool_invocation`** — `session_id fk`, `tool_name`, `arguments (jsonb)`,
  `attempt_number`, `status` (`pending|succeeded|failed|retrying`),
  `latency_ms`, `error_message`, `started_at`, `finished_at`. One row per
  attempt, so retries are visible individually, not overwritten.
- **`claim_draft`** / **`claim_submission`** — the accumulated claim
  fields (documents gathered, computed acceptance-probability score) and
  the final immutable submission record once `submit_claim` succeeds.
- **`approval_request`** — `session_id fk`, `claim_draft_id fk`, `status`
  (`pending|approved|rejected`), `decided_by`, `decided_at`. The audited
  HITL gate — a distinct row and a distinct state transition, never an
  implicit "assume yes."
- **`trace_span`** — `session_id fk`, `span_id`, `parent_span_id`,
  `kind` (`agent_run|tool_call|handoff|guardrail`), `payload (jsonb)`,
  `started_at`, `finished_at`. This *is* the custom tracing exporter
  target (§9) — a table, not a vendor integration.

Migrations via Alembic, one linear history, autogenerate diffs reviewed by
hand before commit (autogenerate on jsonb columns is unreliable).

## 4. Session/state store (Redis)

Postgres holds the durable record; Redis holds the **hot, mutable state**
a running agent loop needs on every turn, keyed by `session:{id}`:

```
session:{id}            HASH   status, current_agent, stagnant_turn_count, last_tool_called
session:{id}:history     LIST   rolling window of last N turns (bounded, for LLM context — not the source of truth, turn table is)
approval:{id}            HASH   status, claim_draft_id  (pub/sub target for the HITL pull-based check, §7)
```

Redis is also the pub/sub backbone for `event_bus.py` (§5): when a
background tool call finishes, or a human approves a claim from a
different request than the one waiting on it, the result is published on
`session-events:{id}` and any FastAPI worker holding that session's open
WebSocket relays it to the browser. This is the one place a "Redis /
Postgres for session memory" mention in the assignment cashes out
concretely: Redis is live/ephemeral coordination state, Postgres is the
system of record.

## 5. Web layer

Four entry surfaces, one dependency-injection discipline:

1. **`POST /api/sessions`** — creates a `CallSession`, seeds Redis state,
   returns `{session_id}`. Called once when the frontend loads.
2. **`POST /api/voice/completions`** — the ElevenLabs **custom-LLM**
   webhook target. ElevenLabs owns STT/TTS/turn-taking and calls this
   endpoint, OpenAI-chat-completions-shaped, once per user utterance. This
   endpoint is the *only* place voice touches the agent runtime — it
   resolves the `CallSession` from the request, calls
   `voice_gateway_service.handle_turn(...)`, and streams back completion
   chunks as they arrive from the agent Runner. Because ElevenLabs already
   owns the realtime audio path, this endpoint's latency budget is the
   whole game: it must start streaming tokens before any tool call
   finishes, which is why tool calls never block the first chunk (§6).
3. **`WS /ws/sessions/{id}`** — a *separate* channel the browser frontend
   opens directly (not through ElevenLabs) to receive the live event feed:
   transcript turns, handoff banners, tool-call-in-progress indicators,
   approval-required prompts. This is what makes the HITL panel and the
   "agent is checking billing status..." indicator possible without
   ElevenLabs needing to support arbitrary server push. See
   `request_api_mapping.md` for the full event contract.
4. **`POST /api/approvals/{id}/decision`**, **`GET /api/claims/{id}`** —
   plain REST for the human-review panel.

Every router function's signature is `(body: SomeSchema, session:
DBSessionDep, call_session: CallSessionDep) -> SomeResponseSchema`. No
router touches `db.session` or Redis directly — always through a service
call. This is what keeps `web/` thin enough to unit-test with a single
FastAPI `TestClient` smoke test while the real logic gets tested at the
service layer without any HTTP in the loop at all.

**Auth**: a single static API-key header dependency
(`web/dependencies/auth.py`) guarding everything under `/api` except
`/api/voice/completions` (ElevenLabs can't easily attach a rotating
header, so that route is instead guarded by a shared secret in the URL
path/query, validated inside the handler). This is intentionally minimal
— multi-tenant auth/roles is out of scope for a 3-day take-home and would
be over-engineering relative to what's graded.

## 6. Concurrency & background execution — deliberately no task queue

This is the one place this design departs hardest from the instinct of reaching for Celery/RQ/Arq whenever something needs
to happen "in the background." **Do not do that here.** Reasons:

- A worker-queue architecture introduces a broker hop and a separate
  worker process. That adds latency and a second failure domain to a path
  where the assignment's explicit grading criterion is *sub-3-second,
  non-blocking tool latency inside a live voice turn*. Dispatch-to-broker,
  worker-picks-up, worker-writes-result, poll-or-callback-back is strictly
  slower and harder to reason about under load than an
  `await`ed coroutine on the same event loop.
- Nothing in this system needs cross-process durability for a job that
  survives a server restart — mock tool calls are idempotent-ish
  simulations with a few seconds of latency, not multi-minute batch jobs.
  A Celery broker (Redis/RabbitMQ + result backend) is solving a problem
  ("this job might outlive this process") that doesn't exist here.
- Task-queue serialization (pickle/json of task args) and the
  broker round-trip are pure overhead for a payload that's just
  `{tool_name, arguments, session_id}`.

Instead, **the concurrency primitive is `asyncio` on the single FastAPI
event loop**:

- Each mock tool call is `await`ed as a plain coroutine
  (`asyncio.sleep(latency)` + optional injected failure). The agents SDK
  already runs tool calls as awaitables inside the agent loop, so "don't
  block the websocket" simply means: never call a tool synchronously
  (`time.sleep`, blocking `requests.get`, sync DB driver) anywhere in the
  tool layer, and let multiple sessions' tool calls interleave naturally
  on the event loop.
- Where a turn needs to *say something* before a tool resolves ("let me
  check that for you...") — because 2–3s of dead air is not acceptable for
  voice — the agent emits a short filler response immediately, then
  `asyncio.create_task()`s the tool call and streams the real answer once
  it resolves, publishing progress over `event_bus` so the frontend can
  show a "checking billing status..." indicator in the meantime. This is
  the *whole* replacement for "background task" in this system: a
  fire-and-forget `asyncio.Task` scoped to that session's lifetime, not a
  cross-process job.
- Concurrency safety across *sessions* (not just within one) is handled by
  never sharing mutable state outside of Redis/Postgres keyed by
  `session_id` — no module-level dict, no global agent instance reused
  across sessions. Each turn resolves its own `CallSession` + Redis hash
  and operates only on that.
- Multi-worker horizontal scaling (multiple uvicorn workers/processes) is
  supported by the Redis pub/sub in `event_bus.py`: if a human approves a
  claim via a request that lands on worker B while the session's WebSocket
  is held open on worker A, the approval event is published on Redis and
  worker A's WS connection relays it — this is the only place Redis
  substitutes for what a task queue would otherwise be used for
  (cross-process notification), and it's pub/sub, not a job queue.
- Retries for a failing tool (`submit_claim` 500s) are also just an
  `asyncio`-native loop: bounded retry count with exponential backoff
  (`asyncio.sleep(backoff)`), each attempt logged as its own
  `tool_invocation` row, and a hard ceiling after which the tool layer
  raises a typed `ToolExhaustedError` that the agent turns into a spoken
  fallback + escalation rather than looping forever.

If a future version of this system needed genuinely long-running,
crash-durable background jobs (e.g. nightly claim-probability model
retraining), that's the point where a task queue would earn its keep — but
introducing one now, for 2–3 second mock-tool latency in a live voice
loop, would be solving a problem this system doesn't have.

## 7. Agent orchestration (services/agents/)

Built on the `openai-agents` SDK, `base_url` pointed at OpenRouter.

- **`triage_agent.py`**: entry point for every new turn on a session with
  no assigned specialist yet. Minimal system prompt, cheap model, one job —
  classify intent and call `handoff()` to `policy_agent` or `claims_agent`.
- **`policy_agent.py`**: owns `get_policy_details`, `get_hospital_network`,
  `get_billing_status`.
- **`claims_agent.py`**: owns document-gathering dialogue,
  `calculate_claim_probability`, and — gated by HITL — `submit_claim`.

`handoff()` (full control transfer) is used rather than
agents-as-tools (sub-call that returns control), because each specialist
should own the conversation for the rest of its arc rather than bouncing
back to triage after every tool call — matches the assignment's "robust
handoff pattern" requirement and avoids re-running triage classification
on every turn (also a token-budget win).

Every handoff is intercepted in one place (`runtime.py`) to write the
`agent_handoff` row and publish a `handoff` event on `event_bus` *before*
control actually passes — so the transition is logged as a fact, not
reconstructed from noticing `turn.agent_name` changed between rows.

**Mock tools** (`services/tools/`): every tool function is wrapped by a
single decorator (`tools/base.py`) that:
1. injects latency (`random.uniform(TOOL_LATENCY_MIN_S, TOOL_LATENCY_MAX_S)`),
2. optionally raises based on `TOOL_FAILURE_RATE` (or a forced-failure flag
   for deterministic tests),
3. writes the `tool_invocation` row (start + finish/error),
4. applies the retry/backoff/escalation policy from §6.

This keeps every individual tool function (`get_billing_status`,
`calculate_claim_probability`, `submit_claim`, `get_policy_details`,
`get_hospital_network`) a plain 5-line async function with the fault
injection, logging, and retry policy factored out — new tools inherit
correctness by construction instead of re-implementing latency/retry
per tool.

**HITL gate**: `submit_claim` is not a normal tool call. `claims_agent`
calls `approval_service.request_approval(claim_draft)` instead of the raw
tool, which creates the `approval_request` row (status=`pending`),
publishes an `approval_required` event to the frontend, and returns
control to the agent with an instruction to tell the caller their claim is
queued for review. The *actual* `submit_claim` tool call only fires from
`approval_service.decide(approval_id, decision)` once a human hits
Approve on the REST endpoint — i.e. the tool call and the human decision
are on two different call stacks, joined only through the
`approval_request` row and the `event_bus` notification back into the
still-open session. This is the pull-based HITL pattern: no assumption
that we can push an arbitrary message into an in-progress ElevenLabs call;
the confirmation is delivered over the frontend WS channel (and,
optionally, as the opening line of the agent's next spoken turn).

## 8. Guardrails / drift detection (services/agents/guardrails.py,
services/drift_service.py)

Three cheap, deterministic checks — not a second LLM judge, to protect
both latency and the token budget:

- **Hallucinated tool**: the agents SDK raises on an unregistered tool
  name already; that's caught, logged as a `trace_span` with
  `kind=guardrail`, and turned into a spoken "I can't do that, let me
  connect you with..." fallback rather than surfacing a raw error.
- **Off-domain drift**: a small keyword/topic allowlist check run on each
  user turn before it reaches the agent (cheap regex/keyword match against
  an insurance-domain term list); turns that miss get one gentle redirect,
  and N consecutive misses force escalation.
- **Stagnation/infinite loop**: `session:{id}.stagnant_turn_count` in
  Redis increments whenever a turn produces the same agent + same tool +
  no new extracted fact as the previous turn, resets on real progress, and
  crossing `DRIFT_MAX_STAGNANT_TURNS` forces a reset-to-triage or a
  spoken handoff to "let me get you a human."

## 9. Observability

- **Structured logging**: one ASGI middleware (`web/middlewares/logging.py`)
  that handles both `http` and `websocket` ASGI scopes in the same class,
  logging one structured access-log line per request/connection with
  `session_id` correlation pulled from the resolved `CallSessionDep`
  wherever available. Handling both scope types in one middleware (rather
  than one for HTTP and hoping WS falls through) is what guarantees the
  voice/WS path gets the same access-log discipline as REST.
- **Tracing**: a custom span exporter, not a vendor SDK default — every
  agent run, tool call, and handoff writes a `trace_span` row with
  `session_id`, `span_id`/`parent_span_id`, timing, and a small JSON
  payload. A single `GET /api/sessions/{id}/trace` endpoint reconstructs
  the full waterfall for a session from that table. Rolling a lightweight
  exporter here (SQL rows, no external tracing vendor) keeps the
  footprint dependency-free and — importantly — means tracing works
  identically whether inference is going through OpenRouter or a stub
  model in tests, since it's not tied to any one provider's dashboard.
- **Error handling**: one exception-handler module maps a small typed
  exception hierarchy (`ToolExhaustedError`, `DriftDetectedError`,
  `SessionNotFoundError`, ...) to a consistent `ErrorModel` JSON shape and
  HTTP status, registered once in `factory.py` — routers just raise
  domain exceptions and never construct `HTTPException` by hand.

## 10. Testing & load-testing hooks this design has to support

(Detailed test/load design is out of scope for this file, but the
architecture above is shaped to make both cheap:)

- Deterministic tests mock the OpenRouter client at the
  `services/agents/runtime.py` boundary (one seam, one fixture) so
  intent → tool-call/handoff assertions never hit a real model or burn
  budget.
- The load test drives `POST /api/voice/completions` (or a lower-level
  `voice_gateway_service.handle_turn` call) concurrently with the *real*
  latency/fault-injected tool layer live, to prove the event loop doesn't
  block and the async DB pool doesn't exhaust — this is exactly why §6
  matters: if tool calls were dispatched to a worker pool instead, the
  load test would be exercising the queue, not the thing the assignment
  asks about (event-loop/connection-pool behavior under concurrent async
  tool calls).

## 11. Deployment shape (docker-compose)

```
services:
  api:        # this FastAPI app, uvicorn, single image
  postgres:   # durable session/claim/trace records
  redis:      # hot session state + pub/sub event bus
  frontend:   # static file server for the lightweight UI (see frontend.md)
```

No worker/beat services — deliberately, per §6.
