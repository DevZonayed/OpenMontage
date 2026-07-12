# Backlot ↔ Remotion Studio — Backend Contract & Gaps

This document records exactly which backend (Worker A) APIs the Remotion production
studio consumes, and the one place where the current backend contract needs an
addition. The studio never touches Python internals or the filesystem — it speaks
only the documented HTTP contract through `src/composition/client.ts`.

## Consumed endpoints (read)

| Endpoint | Used for |
|---|---|
| `GET /api/csrf` | CSRF token for mutations |
| `GET /api/project/{id}/timeline` | canonical `timeline.json` payload → the composition model |
| `GET /api/project/{id}/run` | live production status (phase/state/log) |
| `GET /api/project/{id}/agent-inbox` | queued agent work |
| `GET /api/project/{id}/state` | board state (fallback context) |
| `GET (SSE) /api/project/{id}/events` | live refresh trigger |
| `GET /media/{id}/...` | media playback URLs |

## Consumed endpoints (mutations — CSRF + same-origin)

| Endpoint | Used for |
|---|---|
| `POST /api/project/{id}/timeline` | save the edited timeline (optimistic ETag `if_match`) |
| `POST /api/project/{id}/timeline/render` | render the whole film with the **pinned** Remotion CLI |
| `POST /api/project/{id}/frame` | render a single still (scrub) |
| `POST /api/project/{id}/timeline/revision` | queue a selective regeneration for one layer |
| `POST /api/project/{id}/duration` | change target duration |

## The parity guarantee (no preview/render drift)

All three server render call sites (`lib/frame_render.py`, `lib/timeline_render.py`,
`lib/preview_render.py`) render composition id **`TimelineFrame`** with props
`{timeline, meta}`. The embedded `@remotion/player` renders the **same
`TimelineFrame` component** fed the **same props** (`composition/adapter.ts →
renderProps`). Because it is one component and one data doc, preview and the pinned
CLI render produce identical pixels — this is enforced by
`adapter.test.ts` ("renderProps carries the SAME timeline doc the CLI would render").

`validate_timeline` (Worker A) validates known fields but preserves unknown keys on
save, and `save_timeline` writes the dict verbatim. Therefore the richer render
fields the model adds — `transform`, `fade`, `transitionIn/Out`, `title`,
`subtitle`, per-layer `volume` — are persisted and rendered by the CLI too. No
Worker A change is required for them.

## Contract GAP (documented, not worked around): media URL resolution into the CLI

`timeline.json` `source` must be a **project-local relative path** (enforced by
`lib/timeline.py::_source_is_project_local`). The browser Player can load real media
by resolving that path to a `/media/{id}/...` URL; the composition renders `<Img>` /
`<OffthreadVideo>` / `<Audio>` when a layer's `source` is an **absolute URL**
(`isLoadableUrl`), else a designed placeholder scene.

The headless Remotion CLI render, however, is invoked by Worker A with the raw
relative `source`, which the render bundle cannot resolve — so media layers render
as the designed placeholder in the final CLI output while the live Player (fed
absolute `/media` URLs) shows the real asset.

**This is a data-supply gap in the CLI invocation, not a component divergence.** Given
identical props the component is identical. To close it without changing the
component, Worker A's render invocation should supply media as absolute URLs the
headless browser can fetch, e.g. `http://127.0.0.1:<port>/media/{id}/{path}` (the
Backlot server is already running when a render is triggered), OR pass an
`assetBaseUrl` prop the composition prepends to relative sources. The moment sources
arrive as absolute URLs, the CLI render composites the same real media the Player
shows — no further studio change needed.

## Regenerating the studio bundle

The Backlot UI has no build pipeline, so the built studio bundle is committed at
`backlot/ui/studio.bundle.js` and regenerated with:

```
cd remotion-composer && npm install && npm run build:studio
```
