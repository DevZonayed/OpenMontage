"""Visible, auditable style learning from explicit user approvals/corrections.

  * learn ONLY from explicit choices (source ∈ {approval, correction}); an
    opaque "profiling" source is rejected;
  * provenance, confidence, applied/rejected status recorded;
  * correction supersedes with lineage; reject / delete / reset work;
  * opt-out disables learning (+ optional wipe); privacy is honored;
  * global vs project scope are independent;
  * only the allowed design dimensions (categories) are accepted.

Atomic file store — all paths are tmp_path, never the real global store.
"""

from __future__ import annotations

import pytest

from lib.production_brain.learning import (
    CATEGORIES,
    LearningError,
    StyleLearningStore,
)


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


class TestLearnFromExplicitChoice:
    def test_learn_records_provenance_and_status(self, tmp_path):
        s = _global(tmp_path)
        s.learn(category="pacing", key="cuts_per_min", value=20, source="approval",
                confidence=0.8, run_id="run_1", stage="edit", decision_ref="d-003")
        prefs = s.preferences()
        assert len(prefs) == 1
        p = prefs[0]
        assert p["status"] == "applied"
        assert p["confidence"] == 0.8
        assert p["provenance"]["source"] == "approval"
        assert p["provenance"]["run_id"] == "run_1"
        assert p["provenance"]["decision_ref"] == "d-003"

    def test_opaque_source_is_rejected(self, tmp_path):
        s = _global(tmp_path)
        with pytest.raises(LearningError):
            s.learn(category="pacing", key="x", value=1, source="profiling")

    def test_unknown_category_rejected(self, tmp_path):
        s = _global(tmp_path)
        with pytest.raises(LearningError):
            s.learn(category="colour_grading", key="x", value=1, source="approval")

    def test_all_documented_categories_accepted(self, tmp_path):
        s = _global(tmp_path)
        for cat in CATEGORIES:
            s.learn(category=cat, key="k", value="v", source="approval")
        assert len({p["category"] for p in s.preferences()}) == len(CATEGORIES)

    def test_reapproving_same_key_supersedes_not_duplicates(self, tmp_path):
        s = _global(tmp_path)
        s.learn(category="music", key="genre", value="ambient", source="approval")
        s.learn(category="music", key="genre", value="lofi", source="approval")
        applied = s.preferences(status="applied")
        assert len(applied) == 1 and applied[0]["value"] == "lofi"
        # The prior one is kept (rejected) for the audit trail with a lineage link.
        rejected = s.preferences(status="rejected")
        assert len(rejected) == 1
        assert rejected[0]["provenance"]["superseded_by"] == applied[0]["pref_id"]


class TestCorrectionRejectDelete:
    def test_correction_supersedes_with_lineage(self, tmp_path):
        s = _global(tmp_path)
        s.learn(category="typography", key="font", value="Inter", source="approval")
        first = s.preferences(status="applied")[0]["pref_id"]
        s.correct(first, value="Fraunces", note="user changed their mind")
        applied = s.preferences(status="applied")
        assert len(applied) == 1 and applied[0]["value"] == "Fraunces"
        assert applied[0]["corrects"] == first
        assert applied[0]["provenance"]["source"] == "correction"

    def test_correct_missing_pref_404(self, tmp_path):
        s = _global(tmp_path)
        with pytest.raises(LearningError) as ei:
            s.correct("nope", value="x")
        assert ei.value.status == 404

    def test_reject_marks_status(self, tmp_path):
        s = _global(tmp_path)
        s.learn(category="transitions", key="style", value="hard_cut", source="approval")
        pid = s.preferences()[0]["pref_id"]
        s.reject(pid, note="not for this brand")
        assert s.get(pid)["status"] == "rejected"

    def test_delete_removes(self, tmp_path):
        s = _global(tmp_path)
        s.learn(category="narration", key="tone", value="calm", source="approval")
        pid = s.preferences()[0]["pref_id"]
        s.delete(pid)
        assert s.get(pid) is None
        with pytest.raises(LearningError):
            s.delete(pid)


class TestOptOutAndReset:
    def test_opt_out_disables_learning(self, tmp_path):
        s = _global(tmp_path)
        s.set_opt_out(True)
        s.learn(category="pacing", key="x", value=1, source="approval")  # no-op
        assert s.preferences() == []
        assert s.is_opted_out() is True

    def test_opt_out_with_wipe_clears_existing(self, tmp_path):
        s = _global(tmp_path)
        s.learn(category="pacing", key="x", value=1, source="approval")
        s.set_opt_out(True, wipe=True)
        assert s.preferences() == []

    def test_opt_in_again_allows_learning(self, tmp_path):
        s = _global(tmp_path)
        s.set_opt_out(True)
        s.set_opt_out(False)
        s.learn(category="pacing", key="x", value=1, source="approval")
        assert len(s.preferences()) == 1

    def test_reset_wipes_but_keeps_opt_out(self, tmp_path):
        s = _global(tmp_path)
        s.set_opt_out(True)
        s.learn(category="pacing", key="x", value=1, source="approval")  # no-op anyway
        s.set_opt_out(False)
        s.learn(category="pacing", key="x", value=1, source="approval")
        s.set_opt_out(True)
        s.reset()
        assert s.preferences() == []
        assert s.is_opted_out() is True


class TestScopeIsolation:
    def test_global_and_project_are_independent(self, tmp_path):
        g = _global(tmp_path)
        p = _project(tmp_path)
        g.learn(category="visual_language", key="palette", value="warm", source="approval")
        p.learn(category="scene_density", key="scenes", value=6, source="approval")
        assert len(g.preferences()) == 1 and g.preferences()[0]["scope"] == "global"
        assert len(p.preferences()) == 1 and p.preferences()[0]["scope"] == "project"
        # Cross-check: project write did not bleed into the global store.
        assert all(x["category"] != "scene_density" for x in g.preferences())


class TestPersistence:
    def test_survives_reopen(self, tmp_path):
        s = _global(tmp_path, "persist.json")
        s.learn(category="editing_patterns", key="j_cut", value=True, source="approval")
        reopened = StyleLearningStore(tmp_path / "persist.json", scope="global")
        assert len(reopened.preferences()) == 1
        assert reopened.preferences()[0]["value"] is True


class _StubEvidence:
    def __init__(self, ok: bool):
        self.ok = ok
        self.calls = []

    def verify(self, **kw):
        self.calls.append(kw)
        return self.ok


class TestVerifiedLearning:
    def test_verified_learn_marks_verified_true(self, tmp_path):
        p = _project(tmp_path)
        ev = _StubEvidence(True)
        p.learn(category="pacing", key="cpm", value=18, source="approval",
                run_id="r1", stage="proposal", decision_ref="appr-1",
                require_evidence=True, evidence=ev)
        pref = p.preferences()[0]
        assert pref["provenance"]["verified"] is True
        assert ev.calls[0] == {"run_id": "r1", "stage": "proposal",
                               "decision_ref": "appr-1", "source": "approval"}

    def test_unverifiable_claim_rejected_without_mutation(self, tmp_path):
        p = _project(tmp_path)
        with pytest.raises(LearningError):
            p.learn(category="pacing", key="cpm", value=18, source="approval",
                    run_id="r1", stage="proposal", decision_ref="forged",
                    require_evidence=True, evidence=_StubEvidence(False))
        assert p.preferences() == []

    def test_missing_anchor_rejected(self, tmp_path):
        p = _project(tmp_path)
        for kw in ({"run_id": None}, {"stage": None}, {"decision_ref": None}):
            base = dict(run_id="r", stage="proposal", decision_ref="x")
            base.update(kw)
            with pytest.raises(LearningError):
                p.learn(category="pacing", key="k", value=1, source="approval",
                        require_evidence=True, evidence=_StubEvidence(True), **base)
        assert p.preferences() == []

    def test_no_evidence_source_rejected(self, tmp_path):
        p = _project(tmp_path)
        with pytest.raises(LearningError):
            p.learn(category="pacing", key="k", value=1, source="approval",
                    run_id="r", stage="proposal", decision_ref="x",
                    require_evidence=True, evidence=None)
        assert p.preferences() == []

    def test_promotion_records_verified_provenance(self, tmp_path):
        g = _global(tmp_path)
        g.record_promotion(category="music", key="genre", value="ambient",
                          from_pref="pref_042")
        pref = g.preferences()[0]
        assert pref["provenance"]["source"] == "promotion"
        assert pref["provenance"]["promoted_from"] == "pref_042"
        assert pref["provenance"]["verified"] is True


class TestBrainLogEvidenceIntegration:
    def _seed_run(self, tmp_path, *, reject=False):
        from lib.production_brain.adapter import FakeBrain
        from lib.production_brain.store import ProductionBrainStore

        d = tmp_path / "proj"
        d.mkdir()
        store = ProductionBrainStore(d, gen_id=lambda: "run_e")
        FakeBrain().drive(store, requested_duration_seconds=60, run_id="run_e",
                          approver=(lambda st: False) if reject else "auto")
        ref = None
        wanted = "approval_rejected" if reject else "approval_granted"
        for e in store.read_events_raw():
            if e.get("type") == wanted and e.get("stage") == "proposal":
                ref = (e.get("data") or {}).get("approval_id")
        return d, ref

    def test_verifies_a_real_granted_approval(self, tmp_path):
        from lib.production_brain.evidence import BrainLogEvidence

        d, ref = self._seed_run(tmp_path)
        ev = BrainLogEvidence(d)
        assert ev.verify(run_id="run_e", stage="proposal", decision_ref=ref, source="approval") is True
        # forged ref / wrong stage / wrong run all fail
        assert ev.verify(run_id="run_e", stage="proposal", decision_ref="nope", source="approval") is False
        assert ev.verify(run_id="run_e", stage="assets", decision_ref=ref, source="approval") is False
        assert ev.verify(run_id="other", stage="proposal", decision_ref=ref, source="approval") is False

    def test_rejected_approval_does_not_verify(self, tmp_path):
        from lib.production_brain.evidence import BrainLogEvidence

        d, ref = self._seed_run(tmp_path, reject=True)
        ev = BrainLogEvidence(d)
        assert ev.verify(run_id="run_e", stage="proposal", decision_ref=ref, source="approval") is False
