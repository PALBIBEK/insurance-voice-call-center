# Frontend Design: Lightweight Call-Center Console

Source: `assignment.txt` §Deliverables ("a lightweight frontend to interact
with the system") + `requirement.md` open question #2. This is not a
product UI — it exists to demonstrate the system working end-to-end: talk
to the bot, watch the agent state and tool calls happen live, and act as
the human in HITL. It should look considered, not thrown together, but
every screen has to earn its place against a 2–3 day build budget.

## 1. Scope decision

**Plain HTML/CSS + a small amount of vanilla JS (or a single-file
React-via-CDN component), no build pipeline, no component framework.** A
full SPA framework is not warranted for a handful of widgets — the
justification for a heavier stack (routing, state management, a design
system) only pays off once there are more than a handful of screens. If
the implementation later grows real navigation needs, upgrading is cheap;
starting heavy is not.

There are **two pages, one per role** — the caller and the human reviewer
are different people, and the backend already enforces that split (the
decision API is session-independent REST; see `request_api_mapping.md`):

**`index.html` — caller console** (what the customer sees):

```
┌─────────────────────────────────────────────────────────────┐
│  Top bar: product name · session id · connection status pill │
├───────────────┬───────────────────────────────┬───────────────┤
│  Conversation  │                                │  Activity log │
│  history       │      Live transcript           │   "checking   │
│  sidebar       │      (chat-style bubbles)      │    billing…"  │
├───────────────┴───────────────────────────────┴───────────────┤
│  Mic button / push-to-talk · mute · end call                  │
└─────────────────────────────────────────────────────────────┘
```

**`reviewer.html` — reviewer console** (the human in HITL):

```
┌─────────────────────────────────────────────────────────────┐
│  Top bar: "Reviewer Console" · pending count · backend pill  │
├───────────────────────────────┬───────────────────────────────┤
│   Pending approval cards       │   Recent decisions log        │
│   (policy, amount, docs,       │   "✓ Approved #3 — POL-1001,  │
│    probability bar,            │     ₹ 45,000"                 │
│    Approve / Reject)           │                                │
└───────────────────────────────┴───────────────────────────────┘
```

On the caller page, the left column is the conversation (what the user
did) and the right column is the system's internal state made visible
(what the system is doing). The reviewer page exists to make the HITL
boundary literal: the demo runs with both pages open side by side — a
claim submitted on the caller page appears on the reviewer page, and the
decision flows back to the caller live. The assignment is graded on
demonstrating orchestration, latency-handling, and HITL, and the
frontend's job is to make all three legible at a glance, not to be a nice
consumer chat app.

## 2. Visual language — "light"

Light means: near-white surfaces, no dark chrome, no heavy drop shadows,
generous whitespace, and exactly one accent color doing all the "this
matters" signaling. Concretely:

**Palette** (define as CSS custom properties, one place):

```css
:root {
  --bg:            #F6F7F8;   /* page background — soft off-white, not pure #fff */
  --surface:       #FFFFFF;   /* cards, panels */
  --surface-sunken:#F1F2F4;   /* input bar, code/log blocks */
  --border:        #E3E5E8;   /* hairline borders, never black */
  --text:          #26282B;   /* primary text — near-black, not pure black */
  --text-muted:    #8A8F98;   /* timestamps, secondary labels */
  --accent:        #3A6FF7;   /* primary action, active state, links */
  --accent-soft:   #EAF0FE;   /* accent-tinted backgrounds (active pill, selected state) */
  --success:       #2FA86B;   /* approved / succeeded */
  --warning:       #D79A1F;   /* pending / retrying */
  --danger:        #E0554A;   /* rejected / failed / drift alert */
}
```

- **Typography**: one clean sans-serif (system font stack — `-apple-system,
  "Segoe UI", Inter, sans-serif` — no webfont download needed for a
  throwaway demo UI). Base size 15px, 1.5 line-height, weight 600 only for
  labels/headers, everything else 400.
- **Shape**: 12–16px border-radius on cards and bubbles, 999px (pill) on
  status badges and the mic button. No sharp corners anywhere — it reads
  "modern SaaS," not "form."
- **Elevation**: flat by default. `border: 1px solid var(--border)` does
  the separating work; a shadow only appears on the one truly modal
  element (the approval card), and even then it's a soft, large-radius,
  low-opacity shadow — never a hard drop shadow.
- **Color discipline**: `--accent` is reserved for the mic button, active
  agent-state pill, and links — nothing else competes with it. Status
  colors (success/warning/danger) are used only on small badges/pills,
  never as full backgrounds, so the page stays predominantly neutral even
  when things are actively happening.
- **Density**: padding is generous (16–24px card padding, 12px gap between
  transcript bubbles) — this is a demo/ops console for one user at a time,
  not a data-dense dashboard, so there's no reason to compress it.

## 3. Components

### 3.1 Top bar
Sticky, `--surface` background, hairline bottom border. Left: product
name, plain text, no logo needed. Right: a connection-status pill
(`Connecting…` warning / `Live` success-tinted / `Disconnected` danger)
driven directly off the WebSocket readyState — this is the cheapest
possible way to make "is this thing actually alive" legible, which matters
a lot when the grader's first move is going to be opening this page.

### 3.2 Transcript panel
Standard chat-bubble layout: user turns right-aligned with a subtle
`--accent-soft` fill, agent turns left-aligned on `--surface` with a
border, system/guardrail notices centered as small muted pill-shaped
banners (e.g. "Handed off to Claims Agent", "⚠ Drift detected — resetting
to Triage"). Each agent bubble carries a small `--text-muted` label above
it naming which agent produced it (`Triage`, `Policy`, `Claims`) so a
handoff is visible without reading a system banner — the label itself is
enough signal most of the time, banners are for the harder cases (drift,
error).

Auto-scrolls to bottom on new content unless the user has scrolled up to
read history (standard "stick to bottom" chat behavior — don't yank
scroll position out from under someone reading back).

### 3.3 Handoff visibility
Handoffs are made visible in the transcript itself: each agent bubble's
muted label names the agent that produced it, and a centered pill banner
marks the transfer moment (`Handed off: triage → policy`). No separate
agent-state widget — the transcript already carries the signal, and a
stepper implies a linear pipeline that the actual routing (including
hand-backs from claims to policy) doesn't follow.

### 3.4 Activity log
A short scrolling list of ephemeral, timestamped one-liners driven by the
`tool_call` / `tool_result` / `tool_error` WS events (see
`request_api_mapping.md`): `"Checking billing status…"` → (2.4s later)
`"✓ Billing status retrieved"`, or on failure/retry: `"⚠ submit_claim
failed, retrying (1/3)…"`. Rendered as small muted-text rows, not bubbles
— this is telemetry, visually quieter than the conversation itself but
still legible. This is what makes the 2–3s injected tool latency feel
intentional instead of like the UI hung.

### 3.5 Approval card (HITL) — on the reviewer console
The one place elevation/shadow and a stronger accent border are earned.
Lives on `reviewer.html`, which polls `GET /api/approvals` (the pending
queue across all sessions) every 2s. Each pending approval renders as a
card showing the claim summary (policy #, amount, documents, computed
acceptance-probability score as a percentage bar in
success/warning/danger color depending on the score band), and two
buttons: `Approve` (`--accent` filled) and `Reject` (outlined
`--danger`) that hit `POST /api/approvals/{id}/decision`. The browser
tab title carries the pending count (`(1) Reviewer Console`) so a waiting
approval is visible even when the tab isn't focused. On decision, the
card collapses into a resolved-state line in the console's decisions log
("✓ Approved #3 — POL-1001, ₹ 45,000") rather than just disappearing —
the decision should leave a visible trace, since that audit-visible
moment is explicitly called out as a graded requirement.

The caller page never shows approve/reject controls (a caller approving
their own claim would defeat the gate). It shows the pending state
passively: a transcript banner ("⏳ Claim sent for human review") on
`approval_required`, and the outcome banner + activity-log line when
`approval_decided` / `claim_submitted` arrive over its WebSocket.

### 3.6 Login dialog
The caller console is gated by a modal login overlay (user id + password
against `POST /api/auth/login`; demo credentials `Bibek`/`Bibek`). On
success the returned access token is kept in localStorage and sent as a
Bearer header when creating sessions, binding every conversation to the
verified user — which is what lets agents fetch the caller's profile via
the `get_caller_profile` tool instead of asking for a policy number. The
header shows the signed-in name and a logout button. The reviewer console
is a separate operator surface and is not behind the caller login.

### 3.7 Call controls
A single large pill-shaped mic button, centered in the bottom bar,
three visual states: idle (`--surface` + border), listening (`--accent`
filled, subtle pulse animation), agent-speaking (`--accent-soft` filled,
waveform-style animation or a simple animated equalizer bars icon — cheap
to fake with a few CSS-animated bars, no need for real waveform analysis).
Plus a small `mute` toggle and `end call` (danger-outlined, needs a
confirm because ending a session is not casually reversible mid-claim).

## 4. States to design for explicitly

These map directly onto backend session/tool states in `backend.md` and
`request_api_mapping.md` — the UI should have a visual answer for every
one of them, not just the happy path:

| Backend state | UI treatment |
|---|---|
| WS connecting | top-bar pill = `Connecting…`, mic disabled |
| Listening / user speaking | mic pulses accent, transcript shows live partial text if available |
| Agent speaking | equalizer animation on mic, transcript appends the finalized agent bubble |
| Tool call in flight | activity-log line appears |
| Tool call retrying | activity-log line updates in place with retry count, warning color |
| Tool exhausted / escalation | danger-tinted system banner in transcript: "I'm having trouble with this — connecting you to a human." |
| Handoff | muted system banner in transcript, agent label on subsequent bubbles changes |
| Drift detected | danger-tinted system banner |
| HITL pending | caller: "sent for human review" banner; reviewer console: approval card appears, tab title shows pending count |
| HITL approved/rejected | reviewer console: card resolves to a decisions-log line; caller: outcome banner + activity-log line, transcript gets the agent's follow-up turn |
| Call ended | mic area replaced with a "Start new call" button, transcript stays visible read-only |

## 5. What this explicitly does not need

No auth/login screen (single API key or none, per `backend.md` §7), no
settings page, no history-of-past-calls list, no responsive/mobile
polish beyond "doesn't break at a laptop width," no animation library —
CSS transitions/keyframes are enough for the handful of state changes
above. Any time spent beyond making the states in §4 legible is time not
spent on the orchestration/reliability engineering the assignment is
actually graded on.
