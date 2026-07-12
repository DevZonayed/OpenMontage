# Production Brain — observable Hermes video-production telemetry

The **production brain** makes the Hermes agent a *persistent, transparent*
video-production brain rather than a hidden subprocess. It records a canonical,
versioned, append-only history of a production run so any observer (the Backlot
board, an operator, a test) can always answer:

> *Which agent / job / tool / provider is doing which task, at which stage, with
> what progress, elapsed time, latest event, outputs, approvals, blockers, and
> errors — and is the run truly running, waiting, blocked, cancelled, failed, or
> complete?*

This document is the **stable contract Worker B (and any external consumer)
reads**. It describes the data model, the durability guarantees, the HTTP API,
and the honesty rules. Nothing here fabricates an LLM or generates paid media —
advancing a run through its stages is agent-driven (Rule Zero); the brain is the
telemetry + control plane around that work.

---

## 1. Where it lives

```
lib/production_brain/
  schema.py     # stages, run states, transition table, secret redaction, reducer
  store.py      # ProductionBrainStore — the single writer (durable, atomic)
  adapter.py    # BrainAdapter contract: HermesBrainAdapter (fail-closed) + FakeBrain
  learning.py   # StyleLearningStore — visible style learning from explicit choices

backlot/brain_api.py   # pure read/control functions the server routes call
schemas/artifacts/
  production_run_state.schema.json   # the materialized run-state document
  production_event.schema.json       # one append-only event

projects/<id>/brain/
  run_events.jsonl   # APPEND-ONLY, monotonically-sequenced event history (authoritative)
  state.json         # materialized production_run_state (a rebuildable cache)
  learned_style.json # project-scope learned preferences
```

### Relationship to `run.json` (no duplicate writers)

`lib/production_run.py` owns `projects/<id>/run.json` — the *coarse* "is a local
preflight worker process alive" lifecycle. The production brain owns the two
files under `projects/<id>/brain/` — the *fine-grained, stage-level* production
narrative. **Each file has exactly one writer**; the two layers share a `run_id`
so a UI can correlate them. The brain never writes `run.json` and
`production_run` never writes under `brain/`.

The Backlot watcher **ignores** the `brain/` directory (like it ignores
`run.json`) so high-frequency event writes never flicker the board via SSE. The
board **polls** the brain endpoints, exactly as it polls `/run`.

---

## 2. Data model

### Stages (canonical, ordered)

`research → proposal → script → scene_plan → assets → narration → edit → render →
review → approval → complete`

Each stage carries: `id`, `title`, `status`
(`pending|active|blocked|awaiting_approval|done|failed|skipped`), `progress`
(0..1), `started_at`, `ended_at`, `elapsed_seconds`, `tool`, `provider`,
`job_id`, `latest_event_seq`, `latest_activity`, `outputs[]`, `error`.

### Run states (coarse)

`not_started → running → {awaiting_approval, blocked} → running → …`
terminal: `completed | failed | cancelled`. An active run may go straight to
`cancelled` (the brain cancel is atomic — there is no worker to signal).

### Requested vs actual duration

`requested_duration_seconds` is the user's target (**enforced 1..300**, so 300s ⇒
9000 frames @30fps). `actual_duration_seconds` is the real duration of the
rendered deliverable and is **null until render/complete**. They are always kept
distinct.

### Events

Every state change is an append-only event with a **monotonic `seq`** (the read
cursor), a UTC `ts`, `run_id`, `project_id`, `type`, optional
`stage/agent_id/session_id/job_id/tool/provider`, `message`, and a
**secret-redacted** `data` payload. Event types are enumerated in
`production_event.schema.json`.

**The event log is authoritative.** `state.json` is a materialized view that is
discarded and rebuilt from the log whenever it is missing, torn, or stale — this
is exactly how crash/restart recovery works.

---

## 3. Durability & correctness guarantees

| Guarantee | How |
|---|---|
| Atomic, durable writes | events appended + fsync'd, then `state.json` written via temp+`os.replace`, all under an advisory file lock |
| Monotonic ordering | `seq` assigned under the lock from the max seq in the log |
| Idempotent start | one active run per project; the active-run check and the `run_started` append happen under a **single lock hold**, so two concurrent starts cannot both append — exactly one wins, the rest get `already_active=true` |
| No impossible events | every event is validated against the current run **under the same lock** as its append: an event before a run starts, after it is terminal, or with a mismatched `run_id` is **rejected (409), not persisted**. Strict folding also rejects an impossible coarse-state transition before it reaches the log |
| Project-scoped cancel | validates the **exact** `run_id`; touches only this project |
| Retry / resume | `retry` reopens a failed/blocked stage; `resume` recomputes state from the log and continues |
| Crash recovery | `state.json` is always rebuildable from `run_events.jsonl`; a torn trailing line is skipped |
| Truthful terminal states | terminal runs are sticky — a stray event cannot re-animate a completed/cancelled/failed run |
| No duplicate writers | `brain/*` is written only by `ProductionBrainStore` |
| Secret redaction | keys matching secret patterns and credential-shaped values are masked before persistence; `redacted:true` is stamped |

---

## 4. HTTP API (Backlot)

All reads are **non-blocking snapshots** (safe to poll). All mutations require
the `X-OpenMontage-CSRF` header (from `GET /api/csrf`), reject cross-origin
requests, bound the body size, and are rate-limited (`brain` / `preferences`
buckets).

### Run observability (GET)

| Endpoint | Returns |
|---|---|
| `GET /api/project/{id}/brain` | the full materialized run state (with live `elapsed_seconds` on the active stage) |
| `GET /api/project/{id}/brain/events?after=<seq>&limit=<n>` | a cursor page: `{events, cursor, next_cursor, latest_seq, count, has_more}` |
| `GET /api/project/{id}/brain/assets` | flat roll-up of produced outputs `{outputs, count, actual_duration_seconds}` |

**Live updates:** poll `GET /brain` and `GET /brain/events?after=<cursor>`. The
board's SSE change feed (`/api/project/{id}/events`) still fires on other project
changes; brain telemetry is intentionally poll-only to avoid UI flicker.

### Run control (POST — CSRF + rate `brain`)

| Endpoint | Body | Effect |
|---|---|---|
| `POST /api/project/{id}/brain/start` | `{}` | open a run under the **real Hermes brain**, or `409` if the brain is unavailable (fail-closed). Idempotent. |
| `POST /api/project/{id}/brain/approve` | `{run_id, stage?, approval_id?, note?}` | grant a pending approval gate |
| `POST /api/project/{id}/brain/reject` | `{run_id, stage?, approval_id?, note?}` | reject a gate (marks the stage failed) |
| `POST /api/project/{id}/brain/cancel` | `{run_id}` | cancel the exact active run |
| `POST /api/project/{id}/brain/retry` | `{stage, run_id?}` | reopen a failed/blocked stage |
| `POST /api/project/{id}/brain/resume` | `{}` | reconcile from the log + continue |

Errors are sanitized: `400` (missing field / malformed JSON), `403` (CSRF /
cross-origin), `404` (unknown project), `409` (no active run / wrong `run_id` /
brain unavailable), `413` (body too large), `415` (wrong content-type), `429`
(rate limited).

### Learned style preferences

| Endpoint | Notes |
|---|---|
| `GET /api/project/{id}/preferences?scope=all\|global\|project&category=` | merged view |
| `POST /api/project/{id}/preferences` | `{action, scope, …}` — see below (rate `preferences`) |
| `POST /api/project/{id}/preferences/reset` | `{scope}` wipe a scope |
| `GET /api/preferences` | global preferences only |
| `POST /api/preferences` | global learn/correct/reject/delete/opt_out |

`action` ∈ `learn | correct | reject | delete | opt_out`. **Learning is only
from explicit user choices**: `learn` requires `source ∈ {approval, correction}`
— an opaque `source` (e.g. "profiling") is rejected with `400`. Categories are
fixed: `visual_language, pacing, typography, transitions, narration, music,
scene_density, editing_patterns`. Every preference records provenance
(`source, run_id, stage, decision_ref`), `confidence`, `status`
(`applied|rejected`), and correction lineage (`corrects`, `superseded_by`).
`opt_out` (optionally `wipe:true`) disables all learning for privacy.

---

## 5. The brain adapter (fail-closed)

`lib/production_brain/adapter.py` defines the **secure, explicit** brain contract:

- `HermesBrainAdapter` — the real brain. Availability is probed from the
  subscription-engine layer (`lib.engines`): a signed-in consumer-plan engine is
  the **precondition** for the brain — `available` is *not* a claim that an
  orchestrator is running. If no engine is signed in, `.start()` raises
  `BrainUnavailable` and **no run is opened** (API `409`). When it opens a run it
  attaches a **real, non-secret session + job identity** (a session id is minted
  if the caller supplies none) and stamps `orchestration: "agent_driven"` into
  the brain block. **Honesty:** opening a run does not run an LLM or drive stages —
  OpenMontage has no internal LLM layer; the Hermes *agent* (this session)
  advances stages as it works. The start activity says so explicitly ("agent-driven
  … no autonomous background orchestrator is running") — the API never implies a
  green "brain online" without work actually happening. Identity fields
  (`name, adapter, available, agent_id, session_id, engine, orchestration`) are
  non-secret and stamped onto every event.
- `FakeBrain` — deterministic, offline, **never calls a paid service**. Its
  `.drive(store, requested_duration_seconds, approver=…, stop_after=…)` walks the
  whole stage machine, emitting ordered stage/tool/decision/output/approval
  events. Used by tests and the smoke harness to prove visible stage/task
  changes.

The engine model is truthful: **Hermes** is the brain/orchestrator; **Remotion**,
**HyperFrames**, **FFmpeg**, and the media providers are distinct compositors/
tools recorded as `tool`/`provider` on events. *Installed ≠ render-ready* — a
runtime that is unavailable at its stage surfaces as a `blocker` (kind
`runtime_unavailable`), never a silent swap.

---

## 6. Consuming this from the board (Worker B)

1. On project open, `GET /api/project/{id}/brain`. If `state == "not_started"`,
   show a **Start Production** affordance (`POST …/brain/start`).
2. Render the `stages[]` rail: title, status chip, `progress` bar, `elapsed_seconds`,
   `tool`/`provider`/`job_id`, and `latest_activity`.
3. Show `state`, `current_stage`, `requested_duration_seconds` vs
   `actual_duration_seconds`, `counts`, and `activity` in a header.
4. Tail the event history with `GET …/brain/events?after=<lastCursor>`; append new
   events to a live log; advance `lastCursor = next_cursor`.
5. When a stage is `awaiting_approval`, surface the approval `prompt` and wire
   Approve/Reject to `…/brain/approve` / `…/brain/reject` (pass `run_id`).
6. When `blockers[]` has an unresolved entry, show its `kind`, `message`, and
   `options`; wire Retry to `…/brain/retry`.
7. Provide a Cancel control (`…/brain/cancel` with `run_id`).
8. Surface learned preferences from `GET …/preferences`; let the user correct,
   reject, or opt out.

Poll cadence: `~1–2s` while a run is active (matches the existing `/run` poll).
The endpoints are cheap snapshot reads and never block a server thread.
