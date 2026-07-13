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
  schema.py       # stages, run states, transition table, secret redaction, reducer
  store.py        # ProductionBrainStore — the single writer (durable, atomic)
  orchestrator.py # HermesOrchestratorClient port + Configured (prod) + Fake (test)
  adapter.py      # BrainAdapter: HermesBrainAdapter (real IDs, fail-closed) + FakeBrain
  evidence.py     # BrainLogEvidence — verifies learning claims vs the event log
  learning.py     # StyleLearningStore — verified style learning from explicit choices

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
| Idempotent start | one active run per project; the active-run check, the **external job provisioning**, and the `run_started` append all happen under a **single per-project lock hold**, so of two concurrent starts only the winner ever calls the orchestrator — exactly one external job is created (no orphans); the rest get `already_active=true` |
| Correlated control | every post-`run_started` event (stage/tool/**retry/resume/cancel**/complete) is structurally stamped with the run's persisted external `session_id`/`job_id` from `state["brain"]`, so restart/cancel/retry correlation is machine-verifiable — not message text |
| No impossible events | every event is validated against the current run **under the same lock** as its append: an event before a run starts, after it is terminal, or with a mismatched `run_id` is **rejected (409), not persisted**. Strict folding also rejects an impossible coarse-state transition before it reaches the log |
| Project-scoped cancel | validates the **exact** `run_id`; touches only this project |
| Retry / resume | `retry` reopens a failed/blocked stage; `resume` recomputes state from the log and continues |
| Crash recovery | `state.json` is always rebuildable from `run_events.jsonl`; a torn trailing line is skipped |
| Truthful terminal states | terminal runs are sticky — a stray event cannot re-animate a completed/cancelled/failed run |
| No duplicate writers | `brain/*` is written only by `ProductionBrainStore` |
| No orphan external jobs | if `create_job` succeeds but the local `run_started` write fails, the store invokes a one-shot compensator to cancel the external job and raises a sanitized combined failure — no local run opens, no orphan is silently claimed |
| Truthful cancellation | an external run is only terminal `cancelled` after the orchestrator acknowledges; an unconfirmed cancel is a **non-terminal** `cancelling` + `control_unconfirmed` blocker (survives restart, retryable) |
| Real control | retry/resume/cancel of an external run call the orchestrator FIRST; local state advances only on acknowledgment — a `job_id` field alone never implies control happened |
| Hardened transport | HTTPS-only (loopback-HTTP exception), redirects disabled + 3xx rejected (token never replayed), canonical bounded external ids validated before persistence/URL-encoding |
| Secret redaction | keys matching secret patterns and credential-shaped values are masked before persistence; `redacted:true` is stamped; the orchestrator bearer token lives only in the keyring and never enters telemetry or errors |

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
| `POST /api/project/{id}/brain/start` | `{}` | provision a **real durable orchestrator job** and open the run, or `409` if no orchestrator is available / it returns no canonical ids (fail-closed). Idempotent — a retry keeps the same external job. |
| `POST /api/project/{id}/brain/approve` | `{run_id, stage?, approval_id?, note?}` | grant a pending approval gate |
| `POST /api/project/{id}/brain/reject` | `{run_id, stage?, approval_id?, note?}` | reject a gate (marks the stage failed) |
| `POST /api/project/{id}/brain/cancel` | `{run_id}` | for an external run the orchestrator is cancelled FIRST — only on **acknowledgment** is the run terminal `cancelled`; an unconfirmed external cancel moves it to a **non-terminal `cancelling`** state with a `control_unconfirmed` blocker (retryable) |
| `POST /api/project/{id}/brain/retry` | `{stage, run_id?}` | for an external run the orchestrator is told to `retry` FIRST; local state advances only on ack — on failure the run is `blocked` with a `control_unconfirmed` blocker (no fake local retry) |
| `POST /api/project/{id}/brain/resume` | `{}` | for an external run the orchestrator is told to `resume` FIRST; local state advances only on ack — else a truthful blocker |

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

`action` ∈ `learn | promote | correct | reject | delete | opt_out`.

**Learning evidence is verified, not client-asserted** — enforcement lives in
`StyleLearningStore.learn` itself (project-scope only), not merely in the API. A
project-scope `learn`:
- requires an **explicit** `source ∈ {approval, correction}` — it is NEVER
  defaulted (a missing/opaque source ⇒ `400`);
- requires nonempty `run_id`, `stage`, and `decision_ref`; and
- is verified against the **authoritative append-only event log**. For
  `source="approval"` a non-rejected `approval_granted` for that run+stage,
  referenced by its `approval_id`, must exist. For `source="correction"` a
  **distinct authoritative `correction` event** for that run+stage/ref must exist
  — an approval or a generic `decision` event does **not** count. A forged/
  mismatched ref, a *rejected* approval, or approval-as-correction ⇒ `409`.

A **global** preference cannot be learned directly (`400`): it must be
`promote`d from a **verified** (`provenance.verified == true`) project preference
(`{action:"promote", pref_id}`), or edited via an explicit `correct`. Correct /
reject / delete / reset / opt-out are explicit authenticated user actions that
log auditable provenance. Categories are fixed: `visual_language, pacing,
typography, transitions, narration, music, scene_density, editing_patterns`.
Every preference records provenance (`source, run_id, stage, decision_ref,
verified`), `confidence`, `status` (`applied|rejected`), and lineage (`corrects`,
`superseded_by`, `promoted_from`). `opt_out` (`wipe:true`) disables learning for
privacy.

---

## 5. The orchestration port + brain adapter (fail-closed, real IDs)

`lib/production_brain/orchestrator.py` defines the **secure, explicit
orchestration port** — the brain never fabricates identity:

- `HermesOrchestratorClient` (Protocol) — the injected port. `create_job(...)`
  returns an `OrchestratorHandle{session_id, job_id, engine}` and MUST be
  idempotent on `idempotency_key`; `cancel_job(job_id)` correlates cancellation.
  `kind` is `"live"` or `"fake"`.
- `NativeHermesAgentClient` (in `lib.production_brain.hermes_agent`) — the
  **production** client. OpenMontage is operated natively by the local **Hermes
  Agent** through Hermes's own supported embedding surface, the **ACP stdio
  adapter** (`hermes-acp` / `python -m acp_adapter`; newline-delimited JSON-RPC:
  `initialize` → `session/new` → `session/prompt` / `session/cancel`). There is no
  endpoint, token, project, or job to configure — the agent is auto-detected and
  needs no pasted credentials. Whatever run engine Hermes uses internally is
  invisible to OpenMontage. Hardening:
  - **allowlisted local launch**: the target is the Hermes install under
    `~/.hermes/hermes-agent`, spawned with an **argv list — never a shell** — so
    no caller text can be injected as a command; every subprocess is bounded by a
    timeout and always reaped.
  - **readiness is verified, not assumed** (`HermesAgentDetector.verify`): Hermes's
    own side-effect-free `--check`/`--version` probes must pass before
    `available()` is True. Not installed / not ready / not connected ⇒
    `available() is False` ⇒ Start Production fails closed with an honest "Hermes
    Agent integration not configured" blocker (manual editing always remains).
  - **strict project-path binding**: the ACP session `cwd` is the validated
    OpenMontage repo root, never caller-supplied text.
  - **canonical session id** (`is_canonical_id`): the `session_id` Hermes returns
    must match a strict bounded ASCII allowlist (no slash/backslash/`..`-traversal/
    control/whitespace, ≤128 chars) before it is persisted; it is Hermes's own id,
    never fabricated. A durable, non-secret handle under `.backlot/` makes Start
    idempotent and restart-safe.
  `control_job(job_id, action)` supports `cancel` (via `session/cancel`); native
  retry/resume are not ACP concepts and fail closed honestly. **The real ACP
  transport is not exercised by CI** (an injected `session_factory`/`runner`/
  `canceller` is used instead); a gated smoke exercises the live binary.
- `FakeOrchestratorClient` — deterministic, offline, **TEST-ONLY**. Returns
  canonical-shaped ids derived from the run id and records the start/cancel calls
  it receives. Runs backed by it are visibly `orchestration: "fake_driver"`.

`lib/production_brain/adapter.py`:

- `HermesBrainAdapter(client=…)` — opens a run ONLY after `client.create_job`
  returns canonical `session_id`/`job_id`; those ids are recorded **verbatim**
  (never minted). If the client is unavailable, raises, or returns no valid ids,
  `.start()` raises `BrainUnavailable` and **no run is opened** (API `409`).
  `start` is idempotent (an already-active run keeps its existing external job —
  no second job is provisioned). The run's brain block records
  `orchestration` (`external_job` for a live job, `fake_driver` for the test
  client) and `external: true`; `cancel` correlates with the external handle
  (`cancel_job`). Default (`default_adapter()`) uses the production client, so on
  a machine with no orchestrator configured **Start Production fails closed** —
  it never shows a green run without a real job.
- `FakeBrain` — deterministic, offline, **never calls a paid service**, visibly
  `fake_driver`. Its `.drive(...)` walks the whole stage machine to prove visible
  stage/task changes. Test + smoke only.

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
