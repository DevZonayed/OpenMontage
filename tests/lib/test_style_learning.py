"""Visible, auditable style learning from explicit, VERIFIED user choices.

  * project-scope ``learn`` ALWAYS requires event-log-verified evidence (enforced
    in the store, not only the API); an opaque source is rejected;
  * a GLOBAL preference can never be ``learn``ed directly — only promoted from a
    verified project pref, or edited via explicit correction;
  * correction supersedes with lineage; reject / delete / reset work;
  * opt-out disables learning (+ optional wipe); privacy is honored;
  * only the allowed design dimensions (categories) are accepted;
  * correction evidence requires a DISTINCT authoritative ``correction`` event —
    an approval or a generic decision must not verify correction learning.

Atomic file store — all paths are tmp_path, never the real global store.
"""

from __future__ import annotations

import pytest

from lib.production_brain.learning import (
    CATEGORIES,
    LearningError,
    StyleLearningStore,
)


class _Ev:
    """Stub evidence verifier — returns a fixed verdict, records calls."""

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls = []

    def verify(self, **kw):
        self.calls.append(kw)
        return self.ok


_OK = _Ev(True)


def _clock():
    t = {"n": 0}

    def now():
        t["n"] += 1
        return f"2026-07-12T00:00:{t['n']:02d}+00:00"

    return now


def _global(tmp_path, name="global.json"):
    ids = {"n": 0}

    def gen():
        ids["n"] += 1
        return f"pref_{ids['n']:03d}"

    return StyleLearningStore(tmp_path / name, scope="global", now=_clock(), gen_id=gen)


def _project(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    ids = {"n": 0}

    def gen():
        ids["n"] += 1
        return f"pref_{ids['n']:03d}"

    return StyleLearningStore.project_store(d, now=_clock(), gen_id=gen)


def _plearn(p, *, category, key, value, source="approval", evidence=_OK,
            run_id="r1", stage="proposal", decision_ref="ref1", confidence=0.5, note=None):
    return p.learn(category=category, key=key, value=value, source=source,
                   run_id=run_id, stage=stage, decision_ref=decision_ref,
                   evidence=evidence, confidence=confidence, note=note)


class TestProjectLearnVerified:
    def test_verified_learn_records_provenance(self, tmp_path):
        p = _project(tmp_path)
        _plearn(p, category="pacing", key="cuts_per_min", value=20, confidence=0.8,
                run_id="run_1", stage="edit", decision_ref="d-003")
        prefs = p.preferences()
        assert len(prefs) == 1
        pr = prefs[0]
        assert pr["status"] == "applied" and pr["confidence"] == 0.8
        assert pr["provenance"]["source"] == "approval"
        assert pr["provenance"]["run_id"] == "run_1"
        assert pr["provenance"]["decision_ref"] == "d-003"
        assert pr["provenance"]["verified"] is True

    def test_opaque_source_is_rejected(self, tmp_path):
        p = _project(tmp_path)
        with pytest.raises(LearningError):
            _plearn(p, category="pacing", key="x", value=1, source="profiling")
        assert p.preferences() == []

    def test_unknown_category_rejected(self, tmp_path):
        p = _project(tmp_path)
        with pytest.raises(LearningError):
            _plearn(p, category="colour_grading", key="x", value=1)

    def test_missing_anchors_rejected(self, tmp_path):
        p = _project(tmp_path)
        for missing in ("run_id", "stage", "decision_ref"):
            with pytest.raises(LearningError):
                _plearn(p, category="pacing", key="k", value=1, **{missing: None})
        assert p.preferences() == []

    def test_unverifiable_claim_rejected_without_mutation(self, tmp_path):
        p = _project(tmp_path)
        with pytest.raises(LearningError):
            _plearn(p, category="pacing", key="k", value=1, evidence=_Ev(False))
        assert p.preferences() == []

    def test_no_evidence_source_rejected(self, tmp_path):
        p = _project(tmp_path)
        with pytest.raises(LearningError):
            p.learn(category="pacing", key="k", value=1, source="approval",
                    run_id="r", stage="proposal", decision_ref="x", evidence=None)
        assert p.preferences() == []

    def test_all_documented_categories_accepted(self, tmp_path):
        p = _project(tmp_path)
        for cat in CATEGORIES:
            _plearn(p, category=cat, key="k", value="v")
        assert len({x["category"] for x in p.preferences()}) == len(CATEGORIES)

    def test_rerecording_same_key_supersedes(self, tmp_path):
        p = _project(tmp_path)
        _plearn(p, category="music", key="genre", value="ambient")
        _plearn(p, category="music", key="genre", value="lofi")
        applied = p.preferences(status="applied")
        assert len(applied) == 1 and applied[0]["value"] == "lofi"
        rejected = p.preferences(status="rejected")
        assert len(rejected) == 1
        assert rejected[0]["provenance"]["superseded_by"] == applied[0]["pref_id"]


class TestGlobalLearnForbidden:
    def test_direct_global_learn_raises(self, tmp_path):
        g = _global(tmp_path)
        with pytest.raises(LearningError) as ei:
            g.learn(category="pacing", key="x", value=1, source="approval",
                    run_id="r", stage="s", decision_ref="d", evidence=_OK)
        assert ei.value.status == 400
        assert g.preferences() == []

    def test_promotion_records_verified_provenance(self, tmp_path):
        g = _global(tmp_path)
        g.record_promotion(category="music", key="genre", value="ambient",
                          from_pref="pref_042")
        pref = g.preferences()[0]
        assert pref["provenance"]["source"] == "promotion"
        assert pref["provenance"]["promoted_from"] == "pref_042"
        assert pref["provenance"]["verified"] is True


class TestCorrectionRejectDelete:
    def test_correction_supersedes_with_lineage(self, tmp_path):
        p = _project(tmp_path)
        _plearn(p, category="typography", key="font", value="Inter")
        first = p.preferences(status="applied")[0]["pref_id"]
        p.correct(first, value="Fraunces", note="user changed their mind")
        applied = p.preferences(status="applied")
        assert len(applied) == 1 and applied[0]["value"] == "Fraunces"
        assert applied[0]["corrects"] == first
        assert applied[0]["provenance"]["source"] == "correction"

    def test_correct_missing_pref_404(self, tmp_path):
        p = _project(tmp_path)
        with pytest.raises(LearningError) as ei:
            p.correct("nope", value="x")
        assert ei.value.status == 404

    def test_reject_marks_status(self, tmp_path):
        p = _project(tmp_path)
        _plearn(p, category="transitions", key="style", value="hard_cut")
        pid = p.preferences()[0]["pref_id"]
        p.reject(pid, note="not for this brand")
        assert p.get(pid)["status"] == "rejected"

    def test_delete_removes(self, tmp_path):
        p = _project(tmp_path)
        _plearn(p, category="narration", key="tone", value="calm")
        pid = p.preferences()[0]["pref_id"]
        p.delete(pid)
        assert p.get(pid) is None
        with pytest.raises(LearningError):
            p.delete(pid)


class TestOptOutAndReset:
    def test_opt_out_disables_learning(self, tmp_path):
        p = _project(tmp_path)
        p.set_opt_out(True)
        _plearn(p, category="pacing", key="x", value=1)  # no-op
        assert p.preferences() == []
        assert p.is_opted_out() is True

    def test_opt_out_with_wipe_clears_existing(self, tmp_path):
        p = _project(tmp_path)
        _plearn(p, category="pacing", key="x", value=1)
        p.set_opt_out(True, wipe=True)
        assert p.preferences() == []

    def test_opt_in_again_allows_learning(self, tmp_path):
        p = _project(tmp_path)
        p.set_opt_out(True)
        p.set_opt_out(False)
        _plearn(p, category="pacing", key="x", value=1)
        assert len(p.preferences()) == 1

    def test_reset_wipes_but_keeps_opt_out(self, tmp_path):
        p = _project(tmp_path)
        _plearn(p, category="pacing", key="x", value=1)
        p.set_opt_out(True)
        p.reset()
        assert p.preferences() == []
        assert p.is_opted_out() is True


class TestScopeIsolation:
    def test_global_and_project_are_independent(self, tmp_path):
        g = _global(tmp_path)
        p = _project(tmp_path)
        p.learn(category="scene_density", key="scenes", value=6, source="approval",
                run_id="r", stage="scene_plan", decision_ref="d", evidence=_OK)
        g.record_promotion(category="visual_language", key="palette", value="warm",
                          from_pref="pref_x")
        assert len(g.preferences()) == 1 and g.preferences()[0]["scope"] == "global"
        assert len(p.preferences()) == 1 and p.preferences()[0]["scope"] == "project"
        assert all(x["category"] != "scene_density" for x in g.preferences())


class TestPersistence:
    def test_survives_reopen(self, tmp_path):
        d = tmp_path / "proj"
        d.mkdir()
        s = StyleLearningStore.project_store(d)
        s.learn(category="editing_patterns", key="j_cut", value=True, source="approval",
                run_id="r", stage="edit", decision_ref="d", evidence=_OK)
        reopened = StyleLearningStore.project_store(d)
        assert len(reopened.preferences()) == 1
        assert reopened.preferences()[0]["value"] is True


class TestBrainLogEvidenceIntegration:
    def _seed_run(self, tmp_path, *, reject=False, add_correction=False):
        from lib.production_brain.adapter import FakeBrain
        from lib.production_brain.store import ProductionBrainStore

        d = tmp_path / "proj"
        d.mkdir()
        store = ProductionBrainStore(d, gen_id=lambda: "run_e")
        FakeBrain().drive(store, requested_duration_seconds=60, run_id="run_e",
                          approver=(lambda st: False) if reject else "auto",
                          stop_after="assets" if add_correction else None)
        appr = None
        wanted = "approval_rejected" if reject else "approval_granted"
        for e in store.read_events_raw():
            if e.get("type") == wanted and e.get("stage") == "proposal":
                appr = (e.get("data") or {}).get("approval_id")
        if add_correction:
            store.record_correction("assets", decision_ref="corr-1",
                                    message="user corrected the palette")
        return d, appr, store

    def test_verifies_a_real_granted_approval(self, tmp_path):
        from lib.production_brain.evidence import BrainLogEvidence

        d, ref, _ = self._seed_run(tmp_path)
        ev = BrainLogEvidence(d)
        assert ev.verify(run_id="run_e", stage="proposal", decision_ref=ref, source="approval") is True
        assert ev.verify(run_id="run_e", stage="proposal", decision_ref="nope", source="approval") is False
        assert ev.verify(run_id="run_e", stage="assets", decision_ref=ref, source="approval") is False
        assert ev.verify(run_id="other", stage="proposal", decision_ref=ref, source="approval") is False

    def test_rejected_approval_does_not_verify(self, tmp_path):
        from lib.production_brain.evidence import BrainLogEvidence

        d, ref, _ = self._seed_run(tmp_path, reject=True)
        ev = BrainLogEvidence(d)
        assert ev.verify(run_id="run_e", stage="proposal", decision_ref=ref, source="approval") is False

    def test_correction_needs_a_distinct_correction_event(self, tmp_path):
        from lib.production_brain.evidence import BrainLogEvidence

        d, appr, _ = self._seed_run(tmp_path, add_correction=True)
        ev = BrainLogEvidence(d)
        # A genuine correction event verifies correction learning.
        assert ev.verify(run_id="run_e", stage="assets", decision_ref="corr-1", source="correction") is True
        # An APPROVAL event must NOT masquerade as correction evidence.
        assert ev.verify(run_id="run_e", stage="proposal", decision_ref=appr, source="correction") is False
        # An arbitrary/unknown ref does not verify.
        assert ev.verify(run_id="run_e", stage="assets", decision_ref="ghost", source="correction") is False
        # And the correction ref does NOT satisfy an approval claim either.
        assert ev.verify(run_id="run_e", stage="assets", decision_ref="corr-1", source="approval") is False
