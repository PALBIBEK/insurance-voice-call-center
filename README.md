# Real-Time Autonomous Insurance Call Center

Voice-capable multi-agent call center: triage → policy/billing → claims, with
human-in-the-loop claim approval, injected tool latency/failures with retry +
escalation, drift detection, and per-session tracing.

## Architecture

```
Browser mic/speaker <--> ElevenLabs Conversational AI (STT + TTS + turn-taking)
                              | Custom-LLM webhook (OpenAI-compatible)
                              v
   Browser console <--WS-->  FastAPI service
                              |  POST /api/voice/completions  - per-utterance entry (SSE stream)
                              |  WS   /ws/sessions/{id}       - live event feed to the console
                              |  REST /api/sessions|approvals - lifecycle + HITL + trace
                              |
        +---------------------+----------------------------------------+
        |  GatewayService: guardrails -> agent turn -> persist -> publish
        |  AgentRuntime (first-principles loop, no framework):
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
                    tool attempts,          workers; in-memory fallback
                    approvals, spans)       for single-process dev)
```

Layering: `web/` (routes only) → `services/` (all logic) → `db/` → `config/`.
Detailed design docs: `backend.md`, `frontend.md`, `request_api_mapping.md`.

## Run it

```bash
docker compose up --build -d   # frontend + api + postgres + redis
```

- Caller console: http://localhost:3000 — reviewer console (HITL): http://localhost:3000/reviewer.html
- Log in as `Bibek` / `Bibek` (seeded at startup). Without API keys the service
  runs a rule-based demo brain through the same agents, tools, and HITL flow.
- Demo: ask `What's my billing status?` (triage → policy handoff + tool call),
  then `I was hospitalized and want to file a claim for 45000 rupees` →
  `I have my discharge summary, bills and id proof` → `submit it` — approve or
  reject on the reviewer console and watch the gated submission return live.

**Demo data.** The seeded caller `Bibek` (Bibek Pal, Mumbai) owns policy
`POL-1001` — Family Floater Gold, ₹5,00,000 sum insured, cashless +
reimbursement, premium paid (next due 2026-09-01), required claim documents:
discharge summary, bills, id proof. The caller profile lives in the `user_info`
table (seeded at startup); policy/billing/hospital records live behind the mock
external-system tools — other fixtures to try: `POL-2002` (₹8,450 due),
`POL-3003` (lapsed), hospital networks for Mumbai / Delhi / Bangalore.

**Real keys:** copy `.env.example` → `.env` in the repo root and set
`IVCC_OPENROUTER_API_KEY` (inference) and `IVCC_ELEVENLABS_API_KEY` (voice).
For full duplex voice, point an ElevenLabs Conversational AI agent's Custom LLM
URL at `https://<host>/api/voice/completions?secret=<IVCC_VOICE_WEBHOOK_SECRET>`.

**Local dev without docker:** `cd backend && pip install -e ".[dev]" &&
python -m insurance_voice.asgi`, then `cd frontend && python serve.py`.

## Tests and load test

```bash
cd backend
pytest -q                                 # 75 deterministic tests, no network, no tokens
python scripts/load_test.py --base-url http://localhost:8000 --sessions 30 --waves 2
```

Tests fake the LLM at a single client seam; the real tool layer, retries, HITL
and guardrails run end-to-end. Failure injection demo: set
`IVCC_TOOL_FAILURE_RATE=0.4` and watch retry/backoff + spoken escalation.

Load test result (`reports/load_test_report.txt`, against the docker stack):
**60/60 sessions**, 30 concurrent turns per wave each carrying a 2–3s mock tool
call, ≈3.9s wall per wave (serialized would be ≈150s), p95 3.84s — no blocking,
no pool exhaustion.

## Observability

```bash
curl localhost:8000/health                                  # readiness: db + session store
curl localhost:8000/api/sessions/{session_id}/trace         # spans: agent_run|tool_call|handoff|guardrail|hitl
curl localhost:8000/api/sessions/{session_id}/agent-log     # ordered step-by-step session replay
curl localhost:8000/api/metrics/tools                       # per-tool attempts, success rate, latency
python backend/scripts/agent_logs.py                        # human-readable log of every session
```

`reports/` contains a real exported session (trace + agent log) covering the
full flow — handoffs, tool latencies, probability score, HITL cycle — plus the
load test report and a recorded voice round-trip (STT → agent → TTS).

## Design write-up

**Why not LangGraph — why no framework at all.** Three agents and one approval
gate don't need a graph engine; it adds a second source of truth for control
flow without removing work. The loop (`services/agents/runtime.py`, ~150 lines)
calls the model with the current agent's prompt + tools, executes tool calls
through a policy wrapper, treats `handoff_to_*` as full control transfer, and
caps steps per turn. Owning the loop made the graded requirements direct:
the LLM client is one injectable seam (deterministic, token-free tests),
hallucinated tools are intercepted exactly where calls resolve, and every event
flows through one bus that doubles as the trace exporter. The `openai-agents`
SDK was evaluated — its primitives map 1:1 to this loop, but its tracing
assumes OpenAI's dashboard (inference here routes via OpenRouter) and mocking
its Runner is heavier than faking one `chat.completions.create` surface.

**WebSocket state for real-time voice.** Three channels with distinct jobs:
the ElevenLabs webhook (voice turns), the frontend WS (live events + HITL
prompts), REST (lifecycle + decisions). All per-session mutable state lives in
Redis keyed by session id — no module-level state — so any worker can serve any
request: an approval decided over REST on worker B publishes via Redis pub/sub
and reaches the session's open socket on worker A. Two invariants: **persist,
then publish** (a client reacting to an event can always read the row it
references), and a dropped socket never cancels a turn mid-flight. While a
slow tool runs, the voice stream speaks a filler phrase triggered by the
`tool_call_started` event, then streams the reply word-by-word — the caller
never hears dead air.

**Async safety and cost.** Tool latency is `asyncio.sleep` awaited inside the
loop, so concurrent sessions interleave (proved by the load test). A
Celery-style queue was rejected: a broker hop and second failure domain add
nothing to a sub-3-second in-turn latency budget. Costs stay low via two model
tiers (cheap triage, stronger specialists), short prompts, a bounded history
window, and guardrails that run *before* the LLM.

**Tracing and drift.** `RecordingEventBus` wraps the event bus, so every agent
run, tool attempt (with latency and attempt number), handoff, guardrail, and
HITL step becomes a durable span — the trace cannot drift from what happened.
Spans are a flat time-ordered log (one active turn per session makes temporal
order the causal order). Four drift guards write into the same trace:
hallucinated tools blocked at the call site, off-domain turns gated by a
context-aware classifier before the LLM runs, cross-turn stagnation reset to
triage, and unfulfilled promises ("one moment" with no tool call) nudged into
acting by the runtime.

## Repo map

```
backend/
  insurance_voice/          config/ | db/ (10 tables) | services/ | web/routes.py
    services/agents/        agent definitions + first-principles runtime + demo brain
    services/tools/         registry with latency/failure/retry wrapper + mock APIs
    services/               gateway, approvals (HITL), drift, trace recorder, stores
  tests/                    75 tests (unit + full-stack via TestClient)
  scripts/                  load_test.py, agent_logs.py (readable session logs)
frontend/                   caller + reviewer consoles (static, no build step)
docker-compose.yml          frontend + api + postgres + redis (healthchecked)
.env.example                settings template — empty keys = zero-key demo mode
reports/                    load test report, sample trace exports, voice demo
```
