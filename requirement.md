# Requirements: Real-Time Autonomous Insurance Call Center

Source: `assignment.txt` . This document breaks the
assignment down into concrete engineering requirements, proposes an architecture,
flags design risks that need a decision before coding starts, and lists open
questions for the user/reviewer.

## 1. What is being graded

Not "does a demo run once" — the brief explicitly says they're assessing:
reliability, state management, voice latency, concurrency handling, and
resilience to tool failure. The deliverable is judged as much on the write-up,
tests, and observability as on the happy-path demo.

## 2. Hard constraints

- **Language**: Python.
- **Framework**: anything except LangGraph. `openai-agents` SDK is suggested.
- **Service shape**: must be wrapped as a microservice (FastAPI recommended).
- **Voice**: ElevenLabs — either the Conversational AI WebSocket product or the
  raw Speech (STT/TTS) engine.
- **Inference**: OpenRouter for LLM calls.
- **Budget**: $30 total across OpenRouter + ElevenLabs. Must be provided by the
  assignment issuer — **user needs to request these keys before any live
  integration work can start** (mock/stub everything else in the meantime).
- **AI-assistant logs must be exported and delivered** (Markdown/JSON/HTML) —
  treat every Claude Code session used for this project as part of the
  deliverable; export before wrapping up.

## 3. Functional requirements

### 3.1 Multi-agent orchestration
- **Triage Agent**: entry point, classifies intent, hands off to a specialist.
- **Policy/Billing Agent**: cashless vs. reimbursement, hospital network
  lookup, billing status.
- **Claims Agent**: document collection, claim acceptance probability scoring,
  final submission prep.
- Must use a "robust handoff pattern" — not just an if/else router. With
  `openai-agents` SDK this maps to `handoff()` between `Agent` instances (full
  control transfer) as opposed to "agents-as-tools" (sub-call that returns
  control). Handoffs are the better fit here since each specialist should own
  the conversation once routed.

### 3.2 Human-in-the-loop (HITL)
- Before `submit_claim` actually executes, the system must pause and wait for
  an explicit human approval via a simple frontend/CLI.
- **Design risk (needs resolving before implementation)**: ElevenLabs
  Conversational AI owns turn-taking. If we delegate generation to a custom
  LLM webhook, our backend only gets invoked when the user speaks — there is
  no native "push a message into an active call from an out-of-band event"
  hook guaranteed. Two viable patterns:
  1. **Pull-based (simpler, recommended for v1)**: on reaching the submission
     step, the Claims Agent tells the caller "I've queued this for review,
     I'll confirm shortly" and sets session state to
     `pending_human_approval`. The human approves via a CLI/web panel backed
     by the same FastAPI service. The next user turn (or a short poll loop
     the agent performs) checks approval state before calling `submit_claim`.
  2. **Push-based (stretch goal)**: investigate ElevenLabs' contextual-update
     / agent-initiated message APIs to inject a message into a still-open
     call once a human approves asynchronously. Treat as an enhancement, not
     a blocking requirement.
- Either way, the approval action itself (human clicking "approve") must be a
  distinct, audited state transition — not implicit.

### 3.3 Mock tools & fault injection
Minimum tool set:
- `get_billing_status`
- `calculate_claim_probability`
- `submit_claim`
- (add `get_policy_details`, `get_hospital_network` as needed by the Policy
  agent)

Requirements per tool:
- Injected latency of 2–3s (`asyncio.sleep`), must not block the voice
  websocket or any other concurrent session — i.e. tool calls must be awaited
  concurrently with audio streaming, not run on the same blocking path.
- Simulated failure mode (e.g. `submit_claim` randomly/forced 500s) with a
  defined recovery behavior: retry with backoff, graceful spoken fallback
  ("I'm having trouble submitting this, let me try again..."), and a hard
  ceiling after which the agent escalates rather than looping forever.

### 3.4 Observability, logging, drift detection
- Trace every agent run, tool call, and handoff for a session — needs a
  session/trace ID threaded through the whole call graph.
- `openai-agents` SDK ships built-in tracing, but its default exporter targets
  the OpenAI dashboard and assumes an OpenAI API key/account — since
  inference is routed through OpenRouter, **default tracing export likely
  needs to be disabled or replaced** with a custom processor (e.g. write
  spans to structured logs / SQLite / Postgres table) — verify during
  implementation.
- State transitions (agent handoff, tool call start/end, HITL state changes)
  logged explicitly, not just inferred from LLM output.
- Drift detection — lightweight heuristics, not a second LLM judge (keep
  latency/cost down):
  - hallucinated tool call: agent requests a tool name not in the registered
    schema for its current agent → flag + block execution.
  - off-domain drift: keyword/topic check or a cheap classifier on turns to
    detect the conversation leaving the insurance domain.
  - infinite loop: track consecutive turns without state progress (e.g. same
    agent, same tool, no new info) and cap it, forcing escalation/reset.

### 3.5 Testing & load
- **Deterministic tests**: given a user utterance/intent, assert the correct
  tool call and/or handoff fires. Requires mocking the LLM (deterministic
  fixture responses) rather than hitting OpenRouter in CI, to stay in budget
  and be reproducible.
- **Load test**: concurrent requests against the FastAPI service exercising
  the mock tools' fixed latency, proving no event-loop blocking, no
  connection-pool exhaustion, no crash under concurrency. Locust or a
  `pytest-asyncio` / `asyncio.gather` custom harness both satisfy this —
  lean toward a custom asyncio script for simplicity unless Locust's reporting
  is wanted for the deliverable.
- Test reports (output logs) are a separate deliverable item — capture and
  keep raw output, not just "tests passed."

## 4. Non-functional requirements
- **Latency**: voice must feel real-time; long tool calls must not stall
  audio — implies streaming responses / filler utterances while a tool call
  is in flight.
- **Concurrency safety**: multiple simultaneous call sessions must not share
  or corrupt state; session state needs a store keyed by session/call ID
  (Redis is a natural fit given the docker-compose mention of
  Redis/Postgres).
- **Cost discipline**: explicit ask to not "burn through tokens
  unnecessarily" — implies conscious model choice (cheap/fast OpenRouter
  model for triage and routine turns, reserve anything pricier for
  claim-probability reasoning if needed), short system prompts, and avoiding
  redundant LLM calls per turn.

## 5. Proposed architecture

```
Browser mic/speaker  <-->  ElevenLabs Conversational AI (STT + TTS + turn-taking)
                                  |  (Custom LLM webhook, OpenAI-compatible
                                  |   chat-completions endpoint)
                                  v
                     FastAPI microservice ("the brain")
                     ------------------------------------------------
                     - Custom-LLM endpoint (per-turn entrypoint)
                     - openai-agents SDK runtime:
                         Triage Agent --handoff--> Policy Agent
                                      --handoff--> Claims Agent
                     - Model calls routed to OpenRouter (base_url override)
                     - Mock tool layer (latency + fault injection)
                     - Session/state store (Redis) keyed by call/session id
                     - HITL approval endpoints + minimal review UI/CLI
                     - Tracing/logging middleware (custom span exporter)
                     - Drift-detection guardrail wrapping the agent loop
                                  |
                                  v
                     Postgres/Redis (session + claim state), trace log store
```

Key point: ElevenLabs Conversational AI supports **"Custom LLM"** — you give
it an OpenAI-compatible server URL and it calls that instead of its built-in
model choice, while still owning STT/TTS/interruptions/turn-taking. This lets
us satisfy "use ElevenLabs Conversational AI WebSocket" and "use openai-agents
SDK + OpenRouter for the actual reasoning" simultaneously, instead of having
ElevenLabs' own LLM compete with our agent orchestration. This is the
recommended path; fallback is to skip ElevenLabs Conversational AI entirely
and build a raw STT→agent→TTS pipeline over our own WebSocket using
ElevenLabs' Speech Engine only (more control, more plumbing to write
ourselves).

### Why not LangGraph (for the write-up)
Need to articulate: LangGraph's graph/state-machine model adds orchestration
overhead not needed for a 3-agent handoff topology; `openai-agents` SDK gives
handoffs, tool-calling, and tracing as first-class primitives with less
framework lock-in, and is explicitly designed to be model-provider-agnostic
(works against any OpenAI-compatible endpoint, i.e. OpenRouter) — good fit
for the constraint list. Flesh this out for real once the implementation
choices are finalized.

## 6. Deliverables checklist (map 1:1 to assignment)
- [ ] Code repo, clean/documented Python
- [ ] `docker-compose.yml`: FastAPI service(s) + Redis/Postgres + lightweight
      frontend (HITL approval UI + a way to talk to the voice agent)
- [ ] Exported AI assistant chat logs (this Claude Code session included)
- [ ] `README.md`: architecture diagram, run instructions (app / load tests /
      evals), orchestration write-up (framework choice rationale, websocket
      state management for voice)
- [ ] Test reports: load test output logs

## 7. Open questions / decisions needed before implementation
1. **API keys**: has the OpenRouter/ElevenLabs key request already been sent
   to the assignment issuer? Nothing involving live voice or live inference
   can be validated until keys exist — plan to build/mock against contracts
   first.
2. **Frontend**: how minimal is "lightweight frontend" allowed to be — a
   single HTML page with a mic button + an approval panel, or is a small
   React app expected? Defaults to plain HTML/JS unless told otherwise.
3. **Session store**: Redis vs Postgres vs both (Redis for live session
   state/pubsub, Postgres for durable claim records + trace history)?
   Leaning both, but confirm the actual grading environment can run
   docker-compose with two extra services without friction.
4. **Telephony**: is a real phone number (Twilio etc.) in scope, or is a
   browser-mic WebSocket demo sufficient? Assignment says "voice-capable" and
   "CLI interaction" for HITL, nothing about telephony — assuming browser-only
   is sufficient unless told otherwise.
5. **Tracing backend**: fine to roll a custom lightweight span logger (JSON
   lines / SQLite) rather than integrating a tracing vendor (Langfuse/Phoenix
   etc.)? Keeps footprint small and dependency-free; can add pluggable
   exporter later if wanted.
6. **Model selection on OpenRouter**: any preference (e.g. avoid certain
   vendors), or free choice within the $30 budget? Plan is to default to a
   cheap/fast model for triage/routine dialogue turns and keep an eye on
   spend given the "don't burn tokens" instruction.

## 8. Suggested build order (fits the 2–3 day expectation)
1. Repo scaffold, FastAPI skeleton, docker-compose shell (Redis + app), CI-less
   pytest baseline.
2. Agents + handoffs wired with a fixture/mock model backend (no real
   OpenRouter calls yet) — get triage → policy/claims handoff and mock tools
   (latency + failure injection) working and tested deterministically.
3. Wire real OpenRouter inference once keys are available; keep the mock
   fixture path for CI/tests so the suite doesn't burn budget.
4. HITL approval flow (pull-based) + minimal review UI/CLI.
5. Tracing/logging + drift-detection guardrails.
6. ElevenLabs integration (custom LLM webhook) — smallest possible live-voice
   surface to conserve ElevenLabs budget; validate end-to-end once, then rely
   on text-mode testing for iteration.
7. Load test script + deterministic test suite, capture reports.
8. README (architecture diagram + write-up), export AI chat logs, final pass.
