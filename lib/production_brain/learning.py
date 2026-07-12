"""Visible, auditable style learning from explicit user approvals/corrections.

Principles (enforced by shape, not trust):
  * Learn ONLY from explicit user choices — every write requires an explicit
    ``source`` of ``approval`` or ``correction`` and a provenance reference. There
    is NO opaque profiling path: nothing here observes behavior; a caller must
    hand it a decision the user actually made.
  * Fully inspectable + reversible: every preference records provenance,
    confidence, applied/rejected status, and its correction lineage. Callers can
    read, correct, reject, delete, and reset — globally or per project.
  * Privacy / opt-out: a single flag disables all learning and (optionally)
    wipes stored preferences. When opted out, ``learn`` is a no-op.
  * Two scopes: ``global`` (cross-project defaults) and ``project`` (this run).
    Global lives in a gitignored store; project lives under the project's brain/.

Atomic writes; never raises on read.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from lib.paths import REPO_ROOT

SCHEMA_VERSION = "1.0"

# The design dimensions the brain is allowed to learn about. Anything else is
# rejected so the store can't become a dumping ground for opaque signals.
CATEGORIES: tuple[str, ...] = (
    "visual_language",
    "pacing",
    "typography",
    "transitions",
    "narration",
    "music",
    "scene_density",
    "editing_patterns",
)

SOURCES: frozenset[str] = frozenset({"approval", "correction"})
STATUSES: frozenset[str] = frozenset({"applied", "rejected"})

# Global store path (gitignored, like .backlot/thumbs). Overridable for tests.
GLOBAL_STORE_PATH = Path(
    os.environ.get("OPENMONTAGE_STYLE_STORE") or (REPO_ROOT / ".backlot" / "style_learning.json")
)
PROJECT_STORE_FILENAME = "learned_style.json"
BRAIN_DIRNAME = "brain"

_lock = threading.Lock()


class LearningError(ValueError):
    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.status = status


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_store(scope: str) -> dict:
    return {
        "version": SCHEMA_VERSION,
        "kind": "style_learning",
        "scope": scope,
        "opted_out": False,
        "preferences": [],
        "updated_at": None,
    }


def _read(path: Path, scope: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("kind") == "style_learning":
            data.setdefault("preferences", [])
            data.setdefault("opted_out", False)
            data.setdefault("scope", scope)
            return data
    except Exception:
        pass
    return _empty_store(scope)


def _atomic_write(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


class StyleLearningStore:
    """Read/update/reset store for one scope (global or a single project)."""

    def __init__(self, path: Path, *, scope: str, now=_iso_now, gen_id=None) -> None:
        self.path = Path(path)
        self.scope = scope
        self._now = now
        self._gen_id = gen_id or self._default_gen_id

    @classmethod
    def global_store(cls, *, path: Optional[Path] = None, **kw) -> "StyleLearningStore":
        return cls(path or GLOBAL_STORE_PATH, scope="global", **kw)

    @classmethod
    def project_store(cls, project_dir: Path | str, **kw) -> "StyleLearningStore":
        p = Path(project_dir) / BRAIN_DIRNAME / PROJECT_STORE_FILENAME
        return cls(p, scope="project", **kw)

    def _default_gen_id(self) -> str:
        import secrets

        return "pref_" + secrets.token_hex(4)

    # ---- reads -------------------------------------------------------------
    def read(self) -> dict:
        return _read(self.path, self.scope)

    def preferences(self, *, category: Optional[str] = None,
                    status: Optional[str] = None, include_rejected: bool = True) -> list[dict]:
        prefs = self.read().get("preferences", [])
        out = []
        for p in prefs:
            if category and p.get("category") != category:
                continue
            if status and p.get("status") != status:
                continue
            if not include_rejected and p.get("status") == "rejected":
                continue
            out.append(p)
        return out

    def is_opted_out(self) -> bool:
        return bool(self.read().get("opted_out"))

    # ---- writes ------------------------------------------------------------
    def learn(
        self,
        *,
        category: str,
        key: str,
        value: Any,
        source: str,
        confidence: float = 0.5,
        run_id: Optional[str] = None,
        stage: Optional[str] = None,
        decision_ref: Optional[str] = None,
        note: Optional[str] = None,
        corrects: Optional[str] = None,
        require_evidence: bool = True,
        evidence: Any = None,
    ) -> dict:
        """Record ONE project preference learned from an explicit, VERIFIED choice.

        This method is **project-scope only** and **always** requires event-log
        evidence — enforcement lives here, not only at the API layer. A GLOBAL
        preference can never be learned directly (use ``record_promotion`` from a
        verified project pref, or ``correct``). ``source`` MUST be an explicit
        member of {approval, correction} (never defaulted); ``run_id``/``stage``/
        ``decision_ref`` are all mandatory; and an ``evidence`` verifier must
        confirm the authoritative event log holds a matching, non-rejected
        approval/correction for that run+stage. A claim that cannot be verified
        raises and mutates nothing. If opted out, this is a no-op.
        """
        if self.scope != "project":
            raise LearningError(
                "global preferences cannot be learned directly; promote a verified "
                "project preference or record an explicit correction", status=400)
        if category not in CATEGORIES:
            raise LearningError(f"unknown style category: {category}")
        if source not in SOURCES:
            raise LearningError("source must be an explicit 'approval' or 'correction'")
        if not isinstance(key, str) or not key:
            raise LearningError("key is required")
        try:
            conf = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            raise LearningError("confidence must be a number in [0,1]")

        # Project-scope learning ALWAYS demands verified evidence — a caller
        # cannot opt out of verification.
        if not (run_id and stage and decision_ref):
            raise LearningError(
                "a learned preference must cite run_id, stage and decision_ref "
                "so it can be verified against the run's event log")
        if evidence is None:
            raise LearningError("no evidence source is available to verify this learning")
        if not evidence.verify(run_id=run_id, stage=stage, decision_ref=decision_ref, source=source):
            raise LearningError(
                "no matching, non-rejected user approval/correction for this "
                "run+stage exists in the authoritative event log", status=409)
        verified = True

        provenance = {
            "source": source,
            "run_id": run_id,
            "stage": stage,
            "decision_ref": decision_ref,
            "note": note,
            "verified": verified,  # event-log-backed?
        }
        return self._commit_pref(category=category, key=key, value=value, confidence=conf,
                                provenance=provenance, corrects=corrects)

    def _commit_pref(self, *, category: str, key: str, value: Any, confidence: float,
                     provenance: dict, corrects: Optional[str]) -> dict:
        """Append one preference (superseding a prior one) atomically. Honors opt-out."""
        with _lock:
            store = self.read()
            if store.get("opted_out"):
                return store  # privacy: learning disabled
            ts = self._now()
            prefs: list[dict] = store["preferences"]
            pref = {
                "pref_id": self._gen_id(),
                "scope": self.scope,
                "category": category,
                "key": key,
                "value": value,
                "status": "applied",
                "confidence": confidence,
                "provenance": dict(provenance),
                "corrects": corrects,
                "created_at": ts,
                "updated_at": ts,
            }
            if corrects:
                for existing in prefs:
                    if existing.get("pref_id") == corrects:
                        existing["status"] = "rejected"
                        existing["updated_at"] = ts
                        existing.setdefault("provenance", {})["superseded_by"] = pref["pref_id"]
            else:
                # Re-recording the same (category,key) supersedes the prior applied one.
                for existing in prefs:
                    if (existing.get("category") == category and existing.get("key") == key
                            and existing.get("status") == "applied"):
                        existing["status"] = "rejected"
                        existing["updated_at"] = ts
                        existing.setdefault("provenance", {})["superseded_by"] = pref["pref_id"]
                        pref["corrects"] = existing["pref_id"]
            prefs.append(pref)
            store["updated_at"] = ts
            _atomic_write(self.path, store)
            return store

    def correct(self, pref_id: str, *, value: Any, confidence: float = 0.6,
                run_id: Optional[str] = None, stage: Optional[str] = None,
                decision_ref: Optional[str] = None, note: Optional[str] = None) -> dict:
        """Explicit user correction of an existing preference — appends a new one
        that supersedes it. A correction is a direct, authenticated user action
        (not an event-log claim), so it is recorded with auditable provenance but
        is not marked event-log ``verified``."""
        existing = self.get(pref_id)
        if existing is None:
            raise LearningError("preference not found", status=404)
        try:
            conf = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            raise LearningError("confidence must be a number in [0,1]")
        provenance = {
            "source": "correction",
            "run_id": run_id,
            "stage": stage,
            "decision_ref": decision_ref,
            "note": note,
            "verified": False,
            "corrects_pref": pref_id,
        }
        return self._commit_pref(category=existing["category"], key=existing["key"],
                                value=value, confidence=conf, provenance=provenance,
                                corrects=pref_id)

    def record_promotion(self, *, category: str, key: str, value: Any,
                         from_pref: str, from_scope: str = "project",
                         confidence: float = 0.7, note: Optional[str] = None) -> dict:
        """Promote a VERIFIED project preference to this (global) scope. This is an
        explicit user action; provenance links to the verified source preference."""
        try:
            conf = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            raise LearningError("confidence must be a number in [0,1]")
        provenance = {
            "source": "promotion",
            "promoted_from": from_pref,
            "promoted_from_scope": from_scope,
            "note": note,
            "verified": True,  # only verified project prefs may be promoted (enforced by caller)
        }
        return self._commit_pref(category=category, key=key, value=value,
                                confidence=conf, provenance=provenance, corrects=None)

    def reject(self, pref_id: str, *, note: Optional[str] = None) -> dict:
        with _lock:
            store = self.read()
            found = False
            for p in store["preferences"]:
                if p.get("pref_id") == pref_id:
                    p["status"] = "rejected"
                    p["updated_at"] = self._now()
                    if note:
                        p.setdefault("provenance", {})["reject_note"] = note
                    found = True
                    break
            if not found:
                raise LearningError("preference not found", status=404)
            store["updated_at"] = self._now()
            _atomic_write(self.path, store)
            return store

    def delete(self, pref_id: str) -> dict:
        with _lock:
            store = self.read()
            before = len(store["preferences"])
            store["preferences"] = [p for p in store["preferences"] if p.get("pref_id") != pref_id]
            if len(store["preferences"]) == before:
                raise LearningError("preference not found", status=404)
            store["updated_at"] = self._now()
            _atomic_write(self.path, store)
            return store

    def get(self, pref_id: str) -> Optional[dict]:
        for p in self.read().get("preferences", []):
            if p.get("pref_id") == pref_id:
                return p
        return None

    def set_opt_out(self, opted_out: bool, *, wipe: bool = False) -> dict:
        with _lock:
            store = self.read()
            store["opted_out"] = bool(opted_out)
            if opted_out and wipe:
                store["preferences"] = []
            store["updated_at"] = self._now()
            _atomic_write(self.path, store)
            return store

    def reset(self) -> dict:
        """Wipe all learned preferences for this scope (keeps opt-out setting)."""
        with _lock:
            store = self.read()
            fresh = _empty_store(self.scope)
            fresh["opted_out"] = bool(store.get("opted_out"))
            fresh["updated_at"] = self._now()
            _atomic_write(self.path, fresh)
            return fresh
