# Real-Time Autonomous Insurance Call Center

A voice-capable multi-agent call center: triage → policy/billing → claims,
with human-in-the-loop claim approval, injected tool latency/failures with
retry + escalation, drift detection, per-session tracing, and a live
web console.

## Architecture

```
Browser mic/speaker <--> ElevenLabs Conversational AI (STT + TTS + turn-taking)
                              | Custom-LLM webhook (OpenAI-compatible)
                              v
   Browser console <--WS-->  FastAPI service ("the brain")
   (transcript, agent rail,   |
    tool activity, HITL)      |  POST /api/voice/completions  - per-utterance entry (SSE stream)
                              |  WS   /ws/sessions/{id}       - live event feed to the console
                              |  REST /api/sessions|approvals|claims - lifecycle + HITL + trace
                              |
        +---------------------+----------------------------------------+
        |  GatewayService: guardrails -> agent turn -> persist -> publish
        |  AgentRuntime (first principles loop, no framework):
        |     Triage --handoff--> Policy --handoff--> Claims
        |     LLM client seam: OpenRouter | scripted fake | demo rules
        |  ToolRegistry: latency injection + failure injection + retry/backoff
        |  ApprovalService: HITL gate - submit_claim fires ONLY on approval
        |  DriftService: hallucinated-tool / off-domain / stagnation / unfulfilled-promise
        |  RecordingEventBus: every event -> trace_span row (custom tracer)
        +----------------+-----------------------+
                         |                       |
                    Postgres (durable:      Redis (hot session state,
                    sessions, turns,        pub/sub fan-out across
                    handoffs, tool          workers; in-memory fallback
                    attempts, claims,       for single-process dev)
                    approvals, spans)
```

Layering (`insurance_voice/`): `web/` (routes only) → `services/` (all
logic) → `db/` (persistence) → `config/` (typed settings). Design docs:
`backend.md`, `frontend.md`, `request_api_mapping.md`.

## Run it

### Zero-key demo (no API keys needed)

```bash
docker compose up --build      # starts frontend (UI) + api + postgres + redis
# open http://localhost:3000                (caller console)
# open http://localhost:3000/reviewer.html  (reviewer console - HITL approvals)
# api at http://localhost:8000 (the microservice; UIs talk to it over REST/WS)

docker compose up --build -d   # same, detached (frees the terminal)
docker compose logs -f api     # follow app logs when detached
docker compose down            # stop everything (add -v to also wipe pgdata)
```

Without an OpenRouter key the service runs a rule-based demo brain that
drives the exact same agents, tools, handoffs, HITL and events as the
real model.

**Log in** on the caller console first: user id `Bibek`, password `Bibek`
(seeded into the `user_info` table at startup). Agents fetch the caller's
profile (policies, city) on demand through the `get_caller_profile` tool,
so a logged-in caller never dictates their policy number. Then try:

1. `What's my billing status?` — watch triage hand off to Policy, the
   agent fetch the caller profile, then the billing tool run with
   POL-1001 resolved from it — all streaming into the activity log.
2. `I was hospitalized and want to file a claim for 45000 rupees` →
   `I have my discharge summary, bills and id proof` → `submit it` — the
   caller page shows "sent for human review", the approval card appears
   on the **reviewer console** (`/reviewer.html`, open it side by side);
   Approve/Reject there and watch the gated submission flow back to the
   caller live.

### Inspect a session's trace

Every published event — agent runs, tool attempts, handoffs, guardrails,
HITL — is durably recorded (`trace_span` + `agent_turn_log` tables) and
exposed per session. `docker compose ps` shows the api as `healthy` once
`GET /health` confirms Postgres and Redis are reachable. Then, with a
`{session_id}` from the console's session list (or `GET /api/sessions?user_id=Bibek`):

```bash
curl localhost:8000/health                                    # readiness: db + session store
curl localhost:8000/api/sessions/{session_id}/trace           # raw spans: agent_run|tool_call|handoff|guardrail|hitl
curl localhost:8000/api/sessions/{session_id}/agent-log       # ordered step-by-step replay of the session
curl localhost:8000/api/metrics/tools                         # per-tool attempts, success rate, avg latency
```

For a human-readable version of the same data, one script prints every
conversation as an annotated step-by-step story (stdlib only, no install):

```bash
python backend/scripts/agent_logs.py            # all sessions + tool metrics
python backend/scripts/agent_logs.py a26dbb4b   # one session, by id prefix
```

```
=== session a26dbb4b | user=Bibek | status=active | agent=claims ===
   3  user           I want to check the premium status on my policy POL-1001
   4  handoff        triage -> policy: checking premium status on policy POL-1001
   5  tool           get_billing_status({"policy_number": "POL-1001"}) [succeeded, 2234ms]
   6  agent:policy   Your premium status for policy POL-1001 is paid...
  ...
  23  approval_requested {'policy_number': 'POL-1001', 'claim_amount': 120000.0, ... 'probability': 0.97}
  25  approval_decided rejected by reviewer@console
```

`reports/sample_trace.json` / `sample_agent_log.json` hold a real
exported session covering the full graded flow: triage→policy→claims
handoffs, tool calls with injected 2–3s latencies, a claim probability
score, and a complete HITL cycle (approval requested → decided).

Local dev without docker (two processes — backend and UI are separate):

```bash
# terminal 1 - the backend microservice
cd backend
pip install -e ".[dev]"        # or: uv pip install -e ".[dev]"
python -m insurance_voice.asgi # http://localhost:8000, sqlite + in-memory bus

# terminal 2 - the web console (static client, own process)
cd frontend
python serve.py                # http://localhost:3000, calls the API cross-origin
```

The backend never renders UI — it exposes only REST/WS/SSE APIs (CORS
allows the console's origin). As a convenience for single-process demos
it will also serve `frontend/index.html` at `/` when the sibling folder
exists on disk, but the canonical setup is the two processes above.

### With real keys (OpenRouter + ElevenLabs)

Copy `.env.example` → `.env` **in the repo root, next to
docker-compose.yml** (compose reads it automatically; `backend/.env.example`
is the equivalent for local no-docker dev), set `IVCC_OPENROUTER_API_KEY` — inference
now routes through OpenRouter (cheap model for triage, stronger for
specialists). For voice: create an ElevenLabs Conversational AI agent,
set its **Custom LLM** URL to `https://<your-host>/api/voice/completions?secret=<IVCC_VOICE_WEBHOOK_SECRET>`
and it will drive the same brain; the web console keeps mirroring the
conversation via the WS feed.

### Tests

```bash
pytest -q                      # 75 deterministic tests, no network, no tokens
```

The LLM is a scripted fake at the runtime's single client seam; the tool
layer runs real (millisecond-scaled latency policy), so orchestration,
retries, HITL and guardrails are exercised end-to-end.

### Load test

```bash
docker compose up -d                      # zero-key mode recommended: same orchestration path, no token spend
python scripts/load_test.py --base-url http://localhost:8000 --sessions 30 --waves 2
```

Latest report (`reports/load_test_report.txt`, run against the full
docker stack — api+postgres+redis): **60/60 sessions**, 30 concurrent
turns per wave, each carrying one 2–3s mock tool call; wall time ≈3.9s
per wave where fully serialized execution would take ≈150s; p50 3.39s,
p95 3.84s, max 3.96s — the event loop and DB pool never block, no
connection-pool exhaustion.

### Failure-mode demos

```bash
IVCC_TOOL_FAILURE_RATE=0.4 python -m insurance_voice.asgi
```

Tools now 500 randomly: watch retry-with-backoff in the activity log,
one `tool_invocation` row per attempt, and the spoken fallback +
escalation when the retry ceiling is hit.

## Design write-up

**Why not LangGraph, and why no framework at all.** The topology here is
three agents and one gate — a graph engine adds a second source of truth
for control flow without removing any real work. The orchestration loop
(`services/agents/runtime.py`, ~150 lines) is: call the model with the
current agent's prompt + tools; execute tool calls through a policy
wrapper; treat `handoff_to_*` as full control transfer; cap steps per
turn. Owning the loop made three graded requirements nearly free, where a
framework would have made them plugin work: (1) the LLM client is one
injectable seam, so tests are deterministic and token-free; (2)
hallucinated tools are intercepted exactly where the tool call resolves;
(3) every event (handoff, tool attempt, guardrail, HITL) publishes
through one bus that doubles as the trace exporter. The
`openai-agents` SDK was evaluated: its handoff/tool primitives map 1:1
to this loop, but its tracing defaults assume OpenAI's own dashboard (we
route via OpenRouter) and mocking its Runner in tests is heavier than
faking one `chat.completions.create` surface.

**WebSocket / voice state management.** Three channels with distinct
jobs: the ElevenLabs webhook (voice reasoning), the frontend WS
(observability + text mode + HITL prompts), and REST (lifecycle +
decisions). All per-session mutable state lives in Redis (or the
in-memory equivalent) keyed by session id — no module-level state — so
concurrent sessions can't corrupt each other and any uvicorn worker can
serve any request. The approval flow proves this: the REST decision can
land on worker B while the session's WS lives on worker A; the decision
publishes to Redis pub/sub and worker A relays it into the still-open
socket. Ordering rule enforced everywhere: **persist, then publish** — a
client reacting to an event can always read the row it references. A
dropped WS never cancels a turn mid-flight; in-flight turns complete and
persist before the handler exits.

**Async safety without a task queue.** Tool latency is `asyncio.sleep`
awaited inside the agent loop — concurrent sessions interleave on the
event loop (proved by the load test and
`test_concurrent_invocations_interleave_not_serialize`). A worker-queue
(Celery-style) architecture was deliberately rejected: it adds a broker
hop and a second failure domain to a path whose grading criterion is
sub-3-second non-blocking behavior inside a live voice turn, and nothing
here needs cross-process job durability.

**Tracing as a bus decorator, and flat spans on purpose.** Rather than
instrumenting each service, `RecordingEventBus` wraps the one event bus
everything already publishes through, so the trace can never drift out of
sync with what actually happened — if it emitted an event, it's in the
trace. Spans are a flat log ordered by time, not a nested tree
(`parent_span_id` stays null): a session has exactly one active turn at a
time, so temporal order *is* the causal order, and a tree would add
bookkeeping without adding information. Drift guardrails write into the
same trace (`kind: guardrail`) — four checks: hallucinated tools blocked
at the call site, off-domain turns gated by a context-aware classifier
*before* the LLM runs, cross-turn stagnation reset to triage, and
unfulfilled promises ("one moment" with no tool call) caught in the
runtime loop and nudged into acting.

**Cost discipline.** Two model tiers (triage vs specialist), short
system prompts, a bounded rolling history window (30 messages), guardrail
checks that run *before* the LLM (off-domain turns never reach it), and a
zero-token demo/test path for all iteration.

## Repo map

```
backend/                    the microservice (deployable on its own)
  insurance_voice/
    config/settings.py      typed settings (IVCC_ env prefix), CORS origins
    db/models/core.py       10 tables: user_info, session, turn, handoff,
                            tool_invocation, claim_draft/submission, approval,
                            trace_span, agent_turn_log
    services/tools/         registry + latency/failure/retry wrapper + mock tools
                            (incl. get_caller_profile: session -> user_info)
    services/agents/        definitions, first-principles runtime, demo client
    services/auth_service.py  login, HMAC access tokens, demo-user seeding
    services/gateway.py     guardrails -> turn -> persist -> publish
    services/approval_service.py  HITL gate around submit_claim
    services/drift_service.py     off-domain + stagnation detection (context-aware topic gate)
    services/trace_recorder.py    event bus -> trace_span + agent_turn_log exporter
    services/{session_store,event_bus}.py  Redis + in-memory backends
    web/routes.py           REST (+ /api/auth) + SSE voice webhook + WS
  tests/                    75 tests (unit + full-stack via TestClient)
  scripts/load_test.py      concurrency proof, writes reports/
  scripts/agent_logs.py     readable per-session step log via the API (reviewer tool)
  Dockerfile                backend image only (no UI inside)
frontend/                   the web consoles - a separate client process
  index.html                caller console (no build step)
  reviewer.html             reviewer console - HITL approval queue
  serve.py                  standalone static server (port 3000)
docker-compose.yml          frontend + api + postgres + redis
.env.example                compose-level settings template (keys empty = demo mode)
architecture.html           the diagram above as a styled page (open in a browser)
ai_logs/                    AI-assistant conversation logs used to build this (deliverable 3)
reports/                    load test report, sample trace/agent-log exports, voice round-trip demo
```
