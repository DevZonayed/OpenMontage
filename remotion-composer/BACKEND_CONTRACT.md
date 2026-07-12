# Backlot ↔ Remotion Studio — Backend Contract

The Hermes **production brain** (Worker A) is now merged to `main` (`lib/production_brain/**`,
`backlot/brain_api.py`, brain routes in `backlot/server.py`). This is the ACTUAL,
current contract the studio consumes — not a future/generic `/run`. The studio never
touches Python internals or the filesystem; it speaks only the documented HTTP
contract through `src/composition/client.ts` (typed shapes in `src/composition/brain.ts`,
mirroring `lib/production_brain/schema.py`).

## Production-brain endpoints (consumed by the studio's production panel)

Reads (GET, no CSRF, non-blocking snapshots):

| Endpoint | Returns |
|---|---|
| `GET /api/project/{id}/brain` | canonical `production_run_state` (11 stages, live elapsed on the active stage) |
| `GET /api/project/{id}/brain/events?after=<cursor>&limit=<n>` | `{events, cursor, next_cursor, latest_seq, count, has_more}` — cursor paging: pass the previous `next_cursor` back as `after`; `after` means `seq > after` |
| `GET /api/project/{id}/brain/assets` | `{outputs, count, run_id, actual_duration_seconds}` — each output `{kind, path, label, stage, seq}` |
| `GET /api/project/{id}/preferences?scope=all\|global\|project&category=` | `{categories, global?{opted_out,preferences}, project?{opted_out,preferences}}` |
| `GET /api/preferences?scope=global&category=` | global-only preferences |
| SSE `GET /api/project/{id}/events` | generic change trigger only — the panel refreshes via the brain **cursor poll**, not SSE payloads |
| `GET /media/{id}/<path>` · `GET /thumb/{id}/<path>` | asset media / thumbnails resolved from an output `path` |

Control (POST, **CSRF** `X-OpenMontage-CSRF` + same-origin + rate-limited `brain` bucket).
Each returns the full `production_run_state`. Handles are sent **verbatim**:

| Endpoint | Body | Notes |
|---|---|---|
| `POST .../brain/start` | `{}` | uses intake target; **fail-closed 409** `BrainUnavailable` when the orchestrator is unconfigured |
| `POST .../brain/approve` | `{run_id*, approval_id?, stage?, note?}` | `run_id` required + must match active run |
| `POST .../brain/reject` | `{run_id*, approval_id?, stage?, note?}` | marks the gated stage failed |
| `POST .../brain/cancel` | `{run_id*}` | external cancel is confirmed FIRST; **unconfirmed → non-terminal `cancelling` + `control_unconfirmed` blocker**, never terminal `cancelled` |
| `POST .../brain/retry` | `{stage*, run_id?, job_id?}` | `run_id`+`job_id` required/validated for **external** runs |
| `POST .../brain/resume` | `{run_id?, job_id?}` | same handle validation for external runs |
| `POST .../brain/(reset\|)preferences` | `{action, scope, …}` | see learning below |

## Canonical run state (what the panel renders)

`production_run_state` (`schema.py::empty_state` + reducer): `run_id`, `state`
(`not_started`/`running`/`awaiting_approval`/`blocked`/`cancelling`/`cancelled`/`failed`/`completed`;
terminal = last three; `cancelling` is **active, not terminal**), `terminal`,
`brain` `{name, adapter, available, agent_id, session_id, engine, orchestration, external?, job_id?}`,
`requested/actual_duration_seconds`, `current_stage`, the **11 `stages`**
(research → proposal → script → scene_plan → assets → narration → edit → render →
review → approval → complete; each `{id,title,status,progress,started_at,ended_at,
elapsed_seconds,tool,provider,job_id,latest_event_seq,latest_activity,outputs,error}`),
`approvals[]` `{approval_id,stage,status,prompt,…}`, `blockers[]`
`{blocker_id,stage,kind,message,options[],resolved}`, `outputs[]`, `counts`, `cursor`, timestamps.

**Orchestration kind / LIVE vs FIXTURE:** `brain.orchestration` is `"external_job"`
(a real durable orchestrator job — LIVE) or `"fake_driver"` (the deterministic
offline driver — DETERMINISTIC FIXTURE). The panel shows a LIVE badge only when
`orchestration === "external_job"` and it is not on a client-side fixture; otherwise
DETERMINISTIC FIXTURE. Client-side fixtures (`deterministicBrainState`) are used only
when the backend is unreachable/unconfigured and are always labelled — they never
overwrite live data.

## Style learning (visible, auditable, reversible)

`POST /preferences` dispatches on `action`:
- `learn` — REQUIRES `source ∈ {approval, correction}` (never defaulted), `scope:"project"`,
  and the anchors `category, key, value, run_id, stage, decision_ref` — verified against
  the authoritative event log (`BrainLogEvidence`). The UI only sends `learn` after an
  actual approval/correction, with the exact `decision_ref` (= the `approval_id` of an
  `approval_granted` event, or the `decision_ref` of a `correction` event).
- `promote` — `{pref_id}` of a **verified, applied** project preference → global. Global
  preferences can NEVER be learned directly, only promoted from a verified project one.
- `correct` / `reject` / `delete` / `opt_out` — explicit user actions with provenance.
- `reset` (`POST /preferences/reset {scope}`).

Preference object: `{pref_id, scope, category, key, value, status(applied/rejected),
confidence, provenance{source,verified,run_id,stage,decision_ref,note,…}, created_at, updated_at}`.
Categories: visual_language, pacing, typography, transitions, narration, music,
scene_density, editing_patterns.

## Media resolution parity (Player == pinned CLI) — OPERATIONAL

The Remotion Player and the pinned CLI render resolve the SAME project-local
`source` to the SAME loadable URL: `src/composition/media.ts::resolveAssetSrc(source,
{projectId, assetBaseUrl})` → `{assetBaseUrl}/media/{projectId}/{path}`. This is wired
on BOTH sides, not "can pass":

- **Player** — `StudioApp` calls `renderProps(model, {assetBaseUrl: window.location.origin,
  projectId})`; the composition reads `meta.assetBaseUrl`/`meta.projectId` via a media
  context and resolves each `source`.
- **CLI render** — `lib/render_meta.py::build_render_meta(project_dir, base_url=...)`
  adds `projectId` + a **trusted** `assetBaseUrl` to `meta` for EVERY `TimelineFrame`
  render call site: `lib/timeline_render.py::render_timeline_preview` and
  `lib/frame_render.py::render_still` both call it. The Backlot render endpoints
  (`POST /api/project/{id}/frame`, `POST /api/project/{id}/timeline/render`) pass
  `base_url=request.app.state.render_base_url` into those functions.

**Security model for the base (explicit + fail-closed):**
- The base is provided by the **actual server runtime**: `cmd_serve` builds the app
  with `create_app(render_base_url="http://127.0.0.1:<the-bound-port>")` — the EXACT
  port it binds — and `app.state.render_base_url` is that validated base. It is NEVER
  derived from a request `Host` / `X-Forwarded-*` header, and never inferred.
- `create_app` resolves with `require_explicit=True`: if no explicit base and no
  operator config (`BACKLOT_RENDER_BASE_URL` / `BACKLOT_PORT`) is present, it does
  **not** guess the default port — `app.state.render_base_url` is `None` and the
  render endpoints **fail closed with HTTP 503** rather than render (possibly dropping
  media to a placeholder against a wrong port).
- The render functions (`render_still`, `render_timeline_preview`) **fail closed**
  (`{"ok": False, ...}`) if `build_render_meta` cannot produce a valid base — they
  never fall back to empty meta and render a placeholder while reporting success.
- `resolve_render_base_url` accepts only **loopback HTTP** (127.0.0.1 / ::1 / localhost)
  or an explicitly-configured **`BACKLOT_RENDER_BASE_URL` HTTPS** base (operator trust).
  Non-loopback http, other schemes, a non-integer/out-of-range `BACKLOT_PORT`, and any
  path/query/fragment are rejected.
- The port/base is explicitly injectable (`base_url`/`port` params) for tests. A
  standalone lib render (no server) may fall back to the documented loopback default.
- Per-layer path safety is unchanged: `lib.timeline` only persists project-local
  `source`s, and `resolveAssetSrc` rejects `..` traversal and non-loopback schemes.

Proven by: `tests/backlot/test_render_media_parity.py` (captures the real props file
handed to `_rr.render_argv`/`_rr.still_argv` and asserts `meta.{projectId,assetBaseUrl}`;
a direct route test proves the active port is wired), `adapter.test.ts` +
`media.test.ts` (identical transform for Player + render doc), and a real still render
of a project-local red image served via `/media` whose top-region pixels are (254,0,0)
— the real media, not the placeholder.

## Regenerating the studio bundle

The Backlot UI has no build pipeline; the built bundle is committed at
`backlot/ui/studio.bundle.js` and regenerated (with a content-hash stamped into
`editor.html`) via:

```
cd remotion-composer && npm install && npm run build:studio
```
