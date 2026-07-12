# Request ↔ API Mapping

Cross-reference for `backend.md` (module layout) and `frontend.md` (UI
elements/states). Every UI affordance and every backend module boundary
in those two documents is enumerated here against the concrete
endpoint/event that connects them, with request/response shape, the
service that owns the logic, what gets written to Postgres/Redis, and
what trace span it produces. If it isn't in this file, the frontend has
no way to know it happened and the backend has no contract to implement
against — treat this as the source of truth both sides get built from.

There are **three distinct channels**, not one, and mixing them up is the
single most likely design mistake in this system:

1. **Frontend ↔ Backend REST** — session lifecycle, HITL decisions, trace
   lookup. Normal request/response.
2. **ElevenLabs ↔ Backend webhook** — the *voice reasoning* path. ElevenLabs
   owns the audio/STT/TTS/turn-taking and calls our backend once per user
   utterance as if we were an OpenAI-compatible completions endpoint. The
   frontend is **not** a party to this channel at all when running in pure
   voice mode.
3. **Frontend ↔ Backend WebSocket** — the *observability/HITL* path. This
   is how the browser finds out what's happening inside channel 2 (and
   inside text-mode turns) in real time: transcript, agent state, tool
   activity, approval prompts. This channel exists specifically because
   channel 2 is invisible to the browser otherwise.

A text-mode (no ElevenLabs) fallback re-uses channel 3 for the user's
outgoing turns too — see §4.

## 1. Session lifecycle (REST)

| UI trigger | Method & path | Request body | Response body | Owning service | DB / Redis writes |
|---|---|---|---|---|---|
| Page load | `POST /api/sessions` | `{ "channel": "voice" \| "text" }` | `{ "session_id": uuid, "ws_url": str, "status": "active" }` | `session_service.create_session` | INSERT `call_session` (status=active, current_agent=triage); Redis `HSET session:{id}` |
| Page load (resume) | `GET /api/sessions/{id}` | — | `{ "session_id", "status", "current_agent", "started_at" }` | `session_service.get_session` | read-only |
| "End call" button | `POST /api/sessions/{id}/end` | — | `{ "status": "completed" }` | `session_service.end_session` | UPDATE `call_session.status/ended_at`; publish `session_ended` on `event_bus` |

## 2. Voice turn path (ElevenLabs custom-LLM webhook)

This is the channel described in `backend.md` §5.2. ElevenLabs is
configured with our endpoint as its "Custom LLM" base URL, so it calls
this exactly like an OpenAI chat-completions endpoint.

| Trigger | Method & path | Request body (OpenAI-compatible) | Response | Owning service |
|---|---|---|---|---|
| User finishes an utterance (ElevenLabs VAD detects end of speech) | `POST /api/voice/completions` | `{ "model": str, "messages": [...], "stream": true, "metadata": {"session_id": uuid} }` | SSE stream of `chat.completion.chunk` objects, OpenAI-compatible | `voice_gateway_service.handle_turn` |

Internally, `handle_turn`:
1. Resolves `CallSession` from `metadata.session_id`, sets the
   `AppSession` contextvar for this coroutine.
2. Appends the incoming user message to the `turn` table
   (`role=user`) and publishes a `turn_created` event on
   `event_bus` — **this is the only reason the frontend transcript panel
   can show the user's words at all in voice mode**, since the frontend
   never receives the ElevenLabs audio/webhook traffic directly.
3. Runs the current agent (Triage/Policy/Claims) via
   `services/agents/runtime.py`'s `Runner`, which may call tools and/or
   `handoff()` (§3, §5).
4. Streams the agent's textual response back as completion chunks (for
   ElevenLabs to speak) **and** publishes the finalized turn + any
   handoff/tool events on `event_bus` as they occur, so the frontend WS
   sees them in step with — not after — the voice response.

## 3. Frontend realtime channel (WebSocket)

`WS /ws/sessions/{id}` — opened once by the frontend on page load,
right after `POST /api/sessions` resolves. One connection per browser
tab, mapped 1:1 to a `CallSession`. All server→client messages share an
envelope:

```json
{ "type": "<event_type>", "session_id": "uuid", "ts": "iso8601", "data": { ... } }
```

`schema/ws_schema.py` models this as a `Discriminated Union` keyed on
`type`, so both the FastAPI WS handler and any test client get static
typing on every event shape. Full event catalog:

| `type` | `data` payload | Fired when | Frontend UI element it drives (from `frontend.md`) |
|---|---|---|---|
| `connection_ack` | `{ "current_agent", "status" }` | immediately on WS connect | top-bar pill → `Live` |
| `turn_created` | `{ "role": "user"\|"agent", "agent_name": str\|null, "content": str }` | every finalized transcript entry (§2 step 2, or agent's finalized response) | §3.2 transcript panel — appends a bubble |
| `partial_transcript` | `{ "content": str }` | (voice mode, optional/stretch) interim STT text before utterance finalizes | transcript panel — shows greyed "typing"-style partial line |
| `agent_handoff` | `{ "from_agent", "to_agent", "reason": str }` | `agent_handoff` row is written (`backend.md` §7) | §3.2 muted system banner; §3.3 agent-state rail advances |
| `tool_call_started` | `{ "tool_name", "invocation_id", "attempt": int }` | tool wrapper (`tools/base.py`) begins an attempt | §3.4 activity log — new line, e.g. "Checking billing status…"; §3.3 rail node gets spinner ring |
| `tool_call_succeeded` | `{ "tool_name", "invocation_id", "latency_ms" }` | tool wrapper resolves OK | §3.4 activity log line updates to "✓ …" |
| `tool_call_failed` | `{ "tool_name", "invocation_id", "attempt", "will_retry": bool, "error": str }` | tool wrapper catches an injected/real failure | §3.4 activity log line updates to "⚠ retrying (n/max)…" or terminal failure |
| `tool_exhausted` | `{ "tool_name", "invocation_id" }` | retry ceiling hit (`backend.md` §6 last bullet) | §3.2 danger-tinted escalation banner |
| `drift_detected` | `{ "kind": "hallucinated_tool"\|"off_domain"\|"stagnation", "action": "redirected"\|"reset_to_triage"\|"escalated" }` | any guardrail in `drift_service.py`/`guardrails.py` fires | §3.2 danger banner; §3.3 rail resets if `action=reset_to_triage` |
| `approval_required` | `{ "approval_id", "claim_draft": {...}, "probability_score": float }` | `approval_service.request_approval` (HITL gate, `backend.md` §7) | §3.5 approval card slides in |
| `approval_decided` | `{ "approval_id", "status": "approved"\|"rejected", "decided_by": str }` | `approval_service.decide` completes | §3.5 card collapses to resolved log line |
| `claim_submitted` | `{ "claim_id", "status": "submitted"\|"failed" }` | the gated `submit_claim` tool call actually runs post-approval | §3.4 activity log; §3.2 agent's confirmation turn follows via `turn_created` |
| `session_ended` | `{ "reason": str }` | `session_service.end_session` | mic area swaps to "Start new call" |
| `error` | `{ "code", "message" }` | any unhandled exception surfaced to the session | generic danger toast/banner |

Client→server messages on the same socket (used only in **text-mode**,
when there's no ElevenLabs audio channel and the browser is the one
sending user turns):

| `type` | `data` payload | Handled by |
|---|---|---|
| `user_message` | `{ "content": str }` | routed to the same `voice_gateway_service.handle_turn` logic as §2, just entering from the WS instead of the webhook — same agent runtime, same tool layer, same events flow back out. This is *why* `handle_turn` takes a channel-agnostic session + text argument rather than being written against the ElevenLabs request shape directly. |
| `interrupt` | `{}` | (voice-mode parity, stretch) signals the user started talking over the agent; cancels any in-flight non-critical `asyncio.Task` for that session's current turn |

## 4. HITL approval (REST, human-reviewer side)

The approval card in `frontend.md` §3.5 posts here directly (it's plain
REST, not over the session WS, because the reviewer action is a
one-shot decision with a clear response, not a stream):

| UI trigger | Method & path | Request body | Response | Owning service | Side effects |
|---|---|---|---|---|---|
| List pending approvals (if reviewer isn't the same tab as the caller) | `GET /api/approvals?status=pending` | — | `[{ "approval_id", "session_id", "claim_draft", "probability_score", "created_at" }]` | `approval_service.list_pending` | read-only |
| Click "Approve" | `POST /api/approvals/{id}/decision` | `{ "decision": "approved" }` | `{ "approval_id", "status": "approved" }` | `approval_service.decide` | UPDATE `approval_request` (status, decided_at); invokes the real `submit_claim` tool call (§3 tool-call events fire); publishes `approval_decided` + eventually `claim_submitted` on `event_bus` for the *original* session's WS, wherever it's connected (this is the cross-process case Redis pub/sub exists for, `backend.md` §4/§6) |
| Click "Reject" | `POST /api/approvals/{id}/decision` | `{ "decision": "rejected", "reason": str\|null }` | `{ "approval_id", "status": "rejected" }` | `approval_service.decide` | UPDATE `approval_request`; publishes `approval_decided`; agent's next turn acknowledges rejection, no `submit_claim` call ever fires |

## 5. Claims & trace lookup (REST, read-only)

| UI trigger | Method & path | Response | Owning service |
|---|---|---|---|
| (debug/reviewer) inspect a claim | `GET /api/claims/{id}` | `{ "claim_id", "session_id", "fields": {...}, "probability_score", "status" }` | `claims web module` → reads `claim_draft`/`claim_submission` |
| (debug/reviewer) full session waterfall | `GET /api/sessions/{id}/trace` | ordered list of `trace_span` rows reconstructed into a tree (`agent_run` → `tool_call`/`handoff`/`guardrail` children) | `trace_service.get_session_trace` |

## 6. End-to-end sequences

Written out because the *ordering* of events across the three channels
is where this system is easiest to get subtly wrong (e.g. publishing
`turn_created` before the DB write commits, or firing `approval_required`
before the claim draft is actually persisted).

**A. Plain Q&A turn, no tool call** (e.g. "what's cashless vs.
reimbursement?"): webhook receives utterance → `turn_created` (user) →
Policy agent responds directly → `turn_created` (agent) → webhook streams
the same content back to ElevenLabs as completion chunks. No tool events,
no DB writes beyond the two `turn` rows.

**B. Turn with a tool call** (e.g. "what's my billing status?"): webhook
receives utterance → `turn_created` (user) → agent decides to call
`get_billing_status` → `tool_call_started` fires *and* a short filler
phrase streams back to ElevenLabs immediately (`backend.md` §6) →
`asyncio.sleep`-simulated 2–3s → `tool_call_succeeded` → agent's real
answer streams back + `turn_created` (agent).

**C. Tool failure + retry** (`submit_claim` 500, but this pattern applies
to any tool): `tool_call_started` (attempt 1) → `tool_call_failed`
(`will_retry: true`) → backoff sleep → `tool_call_started` (attempt 2) →
… → either `tool_call_succeeded` eventually, or `tool_exhausted` after
`TOOL_MAX_RETRIES`, which the agent turns into a spoken fallback +
`turn_created` (agent, escalation wording).

**D. Handoff**: agent calls `handoff(target_agent)` → `agent_handoff` row
written and `agent_handoff` event published *before* the new agent's
system prompt is invoked → new agent's first response is a normal
`turn_created`. Ordering guarantee: the frontend always sees the rail
advance before the next transcript bubble from the new agent arrives, so
it never looks like "Claims" said something while the rail still shows
"Policy."

**E. Full HITL loop** (the one that spans all three channels): Claims
agent has gathered documents + computed a probability score → calls
`approval_service.request_approval` instead of `submit_claim` directly →
`approval_request` row (status=pending) written → `approval_required`
published on the *session's* WS → agent tells the caller (voice, via the
webhook response) their claim is queued → session state = pending
approval, Redis `session:{id}.status = pending_approval` (new turns from
this session are held/queued rather than re-entering Claims logic) →
separately, reviewer opens the approval card (same tab or a different
browser entirely, doesn't matter) → `POST /api/approvals/{id}/decision`
→ `approval_service.decide` updates the row, and — critically — looks up
the *original* session and publishes back onto `session-events:{session_id}`
in Redis, which is what lets the decision reach a WS connection that may
be held by a **different** uvicorn worker process than the one that
handled the REST call → `approval_decided` reaches the frontend → if
approved, the real `submit_claim` tool call fires through the normal
tool-wrapper path (its own `tool_call_started/succeeded/failed` events)
→ `claim_submitted` → session status flips back to `active` (or
`completed`) and the agent's next voice turn opens with the outcome.

**F. Drift mid-conversation**: any user turn → guardrail check runs
*before* the turn reaches the current agent → if it trips,
`drift_detected` publishes with the specific `kind` and `action` taken →
if `action=reset_to_triage`, an `agent_handoff` event *also* fires
(from current agent back to Triage) so the rail update in §3.3 is driven
by the same single event type as any other handoff, not a special case.

## 7. Contract ownership

`schema/ws_schema.py` and `schema/voice_schema.py` in `backend.md` are the
literal source of truth for every payload shape in §2/§3 above — this
document should be kept in sync with those files, not the other way
around, once implementation starts diverging from the plan.
