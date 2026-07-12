"""Layer AI-regeneration as an honest, queued agent revision request.

The editor's "regenerate this layer with AI" does NOT generate anything itself
(Rule Zero: creative/provider generation is agent-driven). It appends a versioned,
machine-readable ``revision_request`` scoped to ONE layer and marks that layer
``queued`` — the next OpenMontage agent consumes it, does the work, replaces only
that layer's asset, and updates the request status. Nothing here ever claims a
generation happened.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from lib import timeline as _tl

REVISION_FILENAME = "revision_requests.json"
_MAX_PROMPT = 4000
_MAX_KEEP = 200


class RevisionError(ValueError):
    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.status = status


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_gen_id() -> str:
    return "rev_" + secrets.token_hex(6)


def _has_control_chars(s: str) -> bool:
    return any(ord(c) == 0x7F or (ord(c) < 0x20 and c not in "\n\r\t") for c in s)


def list_revisions(project_dir: Path) -> list:
    try:
        data = json.loads((Path(project_dir) / REVISION_FILENAME).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def queue_revision(project_dir: Path, layer_id: str, prompt: str, *,
                   constraints: Optional[dict] = None,
                   gen_id: Optional[Callable[[], str]] = None,
                   now: Optional[Callable[[], str]] = None) -> dict:
    """Append a queued revision request for ONE layer and mark it queued."""
    gen_id = gen_id or _default_gen_id
    now = now or _iso_now
    d = Path(project_dir)

    if not isinstance(prompt, str) or not prompt.strip():
        raise RevisionError("A prompt is required to request a regeneration.")
    if len(prompt) > _MAX_PROMPT:
        raise RevisionError(f"Prompt must be at most {_MAX_PROMPT} characters.")
    if _has_control_chars(prompt):
        raise RevisionError("Prompt contains invalid control characters.")

    tl, tag = _tl.read_timeline(d)
    if tl is None:
        raise RevisionError("This project has no timeline yet.", status=404)
    layer = next((L for L in tl.get("layers", []) if L.get("id") == layer_id), None)
    if layer is None:
        raise RevisionError("That layer no longer exists on the timeline.", status=404)
    if not isinstance(constraints, (dict, type(None))):
        raise RevisionError("Constraints must be an object.")

    ts = now()
    req_id = gen_id()
    request = {
        "id": req_id, "layer_id": layer_id, "layer_type": layer.get("type"),
        "prompt": prompt.strip()[:_MAX_PROMPT],
        "constraints": constraints or {},
        "status": "queued",
        "provenance": {"origin": "editor"},
        "timeline_revision": tag,
        "created_at": ts, "updated_at": ts,
    }

    # 1) Mark the target layer queued on the timeline FIRST (optimistic ETag).
    #    If the timeline changed under us, this raises BEFORE we touch the request
    #    log — so a concurrent edit can never orphan a request (a log entry with no
    #    matching layer marker). Surface it as a clean 409, not an uncaught 500.
    new_layers = []
    for L in tl.get("layers", []):
        if L.get("id") == layer_id:
            L = {**L, "revision": {"status": "queued", "request_id": req_id, "at": ts}}
        new_layers.append(L)
    newtl = {**tl, "layers": new_layers}
    try:
        _tl.save_timeline(d, newtl, if_match=tag)
    except _tl.TimelineError as exc:
        raise RevisionError("The timeline changed while queueing — reload and retry.",
                            status=getattr(exc, "status", 409)) from exc

    # 2) Only after the timeline commit succeeds, append to the machine-readable
    #    request log (bounded). The ETag serializes concurrent queue calls, so the
    #    loser 409s above and never reaches this write.
    reqs = list_revisions(d)
    reqs.append(request)
    reqs = reqs[-_MAX_KEEP:]
    p = d / REVISION_FILENAME
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reqs, indent=2), encoding="utf-8")
    tmp.replace(p)
    return request
