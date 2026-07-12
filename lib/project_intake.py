"""New-project creation + the minimal ``intake.json`` contract.

Creating a project workspace is *operational* (workspace + saved brief); creative
production stays agent-driven per Rule Zero. This module:

  * validates a title / brief / pipeline from the New-Project UI,
  * derives a safe kebab-case project id (collision / traversal / length checked),
  * claims the id atomically and delegates workspace creation to the CANONICAL
    ``lib.checkpoint.init_project`` (never duplicating that logic),
  * writes the non-secret user brief as ``projects/<id>/intake.json`` — the
    smallest documented intake contract (schema in ``INTAKE_SCHEMA_DOC``), and
    rolls back the whole workspace on any failure.

``intake.json`` is what the next agent reads to begin the pipeline. See AGENT_GUIDE
"Project Intake".
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from lib import duration as _duration
from lib.checkpoint import init_project
from lib.paths import PROJECTS_DIR
from lib.pipeline_loader import list_pipelines, load_pipeline_readonly

INTAKE_FILENAME = "intake.json"

# Documented minimal intake contract (v1.1):
INTAKE_SCHEMA_DOC = {
    "version": "1.1 (const)",
    "project_id": "str — the workspace id",
    "title": "str — human title (1..120 chars)",
    "brief": "str — the user's production brief/topic (0..4000 chars, plain text)",
    "pipeline_type": "str — a valid pipeline id from pipeline_defs/",
    "target_duration_seconds": "int — desired story length, 1..300 (canonical; "
        "planning/frame-math/render all derive from it). Absent on legacy "
        "projects → inferred as the documented default.",
    "created_at": "str — ISO-8601 UTC",
}

_MAX_TITLE = 120
_MAX_BRIEF = 4000
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_RESERVED = {".", "..", "_", "history"}


class ProjectIntakeError(ValueError):
    """UI-safe error with an HTTP status. Messages never expose filesystem paths."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _has_control_chars(s: str, *, allow_newlines: bool = False) -> bool:
    for ch in s:
        o = ord(ch)
        if o == 0x7F or (o < 0x20 and not (allow_newlines and ch in "\n\r\t")):
            return True
    return False


def slugify(title: str) -> str:
    """Derive a safe kebab-case id from a title. Raises if nothing usable remains."""
    s = (title or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = s[:64].strip("-")
    if not s or s in _RESERVED:
        raise ProjectIntakeError("Could not derive a valid project id from the title.")
    return s


def validate_pipeline(pipeline: str) -> str:
    if pipeline not in set(list_pipelines()):
        raise ProjectIntakeError("Unknown pipeline. Pick one from the list.")
    return pipeline


def _validate_id(pid: str) -> str:
    if not isinstance(pid, str) or not _ID_RE.match(pid) or pid in _RESERVED or ".." in pid:
        raise ProjectIntakeError("Invalid project id — use lowercase letters, digits, and hyphens.")
    return pid


def list_pipelines_meta() -> list[dict]:
    """Pipeline choices for the UI: id + plain-language purpose + beta flag.

    Sourced from the real pipeline manifests (never a hardcoded divergent catalog).
    """
    out: list[dict] = []
    for name in sorted(list_pipelines()):
        try:
            m = load_pipeline_readonly(name)
        except Exception:
            continue
        stability = str(m.get("stability") or "").lower()
        out.append({
            "id": name,
            "description": (m.get("description") or "").strip(),
            "stability": stability,
            "beta": stability not in ("production",),
        })
    return out


def create_project(
    title: str, brief: str, pipeline: str, *,
    project_id: Optional[str] = None, base: Optional[Path] = None,
    target_duration_seconds: object = None,
) -> dict:
    """Validate + atomically create a project workspace, writing intake.json.

    Returns {"project_id": id}. Raises ProjectIntakeError (with .status). Any
    failure after the id is claimed rolls the whole workspace back.

    ``target_duration_seconds`` is the canonical desired story length; it accepts
    an int/"M:SS"/{minutes,seconds} form, is validated to [1,300], and is
    persisted in intake.json. Omitted → the documented default.
    """
    base = base or PROJECTS_DIR

    # --- validate inputs ---
    if not isinstance(title, str):
        raise ProjectIntakeError("Title is required.")
    title = title.strip()
    if not (1 <= len(title) <= _MAX_TITLE):
        raise ProjectIntakeError(f"Title must be 1–{_MAX_TITLE} characters.")
    if _has_control_chars(title):
        raise ProjectIntakeError("Title contains control characters.")
    brief = brief if isinstance(brief, str) else ""
    if len(brief) > _MAX_BRIEF:
        raise ProjectIntakeError(f"Brief must be at most {_MAX_BRIEF} characters.")
    if _has_control_chars(brief, allow_newlines=True):
        raise ProjectIntakeError("Brief contains invalid control characters.")
    validate_pipeline(pipeline)

    # Canonical duration — reject bad values with an inline, actionable error.
    if target_duration_seconds is None:
        duration_secs = _duration.DEFAULT_TARGET_SECONDS
    else:
        try:
            duration_secs = _duration.parse_duration_input(target_duration_seconds)
        except _duration.DurationError as exc:
            raise ProjectIntakeError(str(exc))

    pid = _validate_id(project_id.strip()) if project_id else slugify(title)

    # --- traversal guard: target must sit directly under base ---
    base.mkdir(parents=True, exist_ok=True)
    target = base / pid
    if target.resolve().parent != base.resolve():
        raise ProjectIntakeError("Invalid project id.")

    # --- atomic id claim (mkdir fails if it already exists) ---
    try:
        target.mkdir(parents=False, exist_ok=False)
    except FileExistsError:
        raise ProjectIntakeError("A project with this id already exists.", status=409)
    except OSError:
        raise ProjectIntakeError("Could not create the project workspace.", status=500)

    try:
        # Canonical workspace creation (subdirs + project.json marker).
        init_project(pid, title=title, pipeline_type=pipeline, pipeline_dir=base)
        intake = {
            "version": "1.1", "project_id": pid, "title": title,
            "brief": brief, "pipeline_type": pipeline,
            "target_duration_seconds": duration_secs,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        tmp = target / (INTAKE_FILENAME + ".tmp")
        tmp.write_text(json.dumps(intake, indent=2), encoding="utf-8")
        os.replace(tmp, target / INTAKE_FILENAME)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)  # atomic rollback
        raise ProjectIntakeError("Could not initialize the project workspace.", status=500)

    return {"project_id": pid}


def set_target_duration(project_dir: Path, seconds: int) -> None:
    """Atomically update ONLY target_duration_seconds in intake.json, preserving
    every other field. Raises ProjectIntakeError if the file is missing/corrupt."""
    p = Path(project_dir) / INTAKE_FILENAME
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        raise ProjectIntakeError("Project intake is missing or unreadable.", status=404)
    if not isinstance(data, dict):
        raise ProjectIntakeError("Project intake is corrupt.", status=500)
    data["target_duration_seconds"] = int(seconds)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def read_intake(project_dir: Path) -> Optional[dict]:
    """Read intake.json, backfilling a canonical ``target_duration_seconds`` for
    legacy/corrupt values IN MEMORY ONLY (never rewrites the file). ``target_duration_inferred``
    is True when the value was defaulted rather than stored."""
    try:
        data = json.loads((project_dir / INTAKE_FILENAME).read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict):
        stored = data.get("target_duration_seconds")
        inferred = _duration.infer_target_seconds(data)
        data["target_duration_seconds"] = inferred
        try:
            data["target_duration_inferred"] = _duration.validate_target_seconds(stored) != inferred
        except _duration.DurationError:
            data["target_duration_inferred"] = True
    return data
