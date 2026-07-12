"""Canonical story-duration contract (target_duration_seconds).

Drives lib/duration.py: a finite integer 1..300 inclusive, exact integer frame
math (seconds*fps), a ~150 wpm narration word budget, mm:ss formatting, tolerant
parsing of UI input forms, and backward-compatible inference. NO hidden demo
length: the default is explicit and documented.
"""

from __future__ import annotations

import math

import pytest

from lib import duration as d


class TestValidate:
    @pytest.mark.parametrize("val", [1, 30, 60, 150, 299, 300, 60.0, 300.0])
    def test_accepts_valid(self, val):
        assert isinstance(d.validate_target_seconds(val), int)

    @pytest.mark.parametrize("val", [0, -1, 301, 1000])
    def test_rejects_out_of_range(self, val):
        with pytest.raises(d.DurationError):
            d.validate_target_seconds(val)

    @pytest.mark.parametrize("val", [True, False])
    def test_rejects_bool(self, val):
        # bool is an int subclass in Python — must be refused explicitly.
        with pytest.raises(d.DurationError):
            d.validate_target_seconds(val)

    @pytest.mark.parametrize("val", [60.5, 0.5, 149.9])
    def test_rejects_fraction(self, val):
        with pytest.raises(d.DurationError):
            d.validate_target_seconds(val)

    @pytest.mark.parametrize("val", [float("nan"), float("inf"), float("-inf")])
    def test_rejects_nonfinite(self, val):
        with pytest.raises(d.DurationError):
            d.validate_target_seconds(val)

    @pytest.mark.parametrize("val", [None, [], {}, "abc", "6e2", "", "60.5"])
    def test_rejects_bad_types_and_strings(self, val):
        with pytest.raises(d.DurationError):
            d.validate_target_seconds(val)

    def test_integral_float_coerced_to_int(self):
        assert d.validate_target_seconds(150.0) == 150


class TestFrameMath:
    @pytest.mark.parametrize("secs,frames", [(60, 1800), (150, 4500), (300, 9000), (1, 30)])
    def test_exact_frames_30fps(self, secs, frames):
        assert d.frames_for(secs, fps=30) == frames

    def test_frames_other_fps(self):
        assert d.frames_for(10, fps=24) == 240
        assert d.frames_for(10, fps=60) == 600

    def test_frames_is_exact_integer(self):
        f = d.frames_for(150, fps=30)
        assert isinstance(f, int) and f == 4500

    def test_frames_validates_input(self):
        with pytest.raises(d.DurationError):
            d.frames_for(0, fps=30)
        with pytest.raises(d.DurationError):
            d.frames_for(301, fps=30)


class TestWordBudget:
    def test_150wpm(self):
        # ~150 words/min → 60s ≈ 150 words, 300s ≈ 750 words
        assert d.word_budget(60) == 150
        assert d.word_budget(300) == 750
        assert d.word_budget(150) == 375


class TestFormat:
    @pytest.mark.parametrize("secs,text", [(30, "0:30"), (60, "1:00"), (150, "2:30"), (300, "5:00"), (5, "0:05")])
    def test_mmss(self, secs, text):
        assert d.format_mmss(secs) == text


class TestParseInput:
    def test_parses_int(self):
        assert d.parse_duration_input(150) == 150

    def test_parses_mmss_string(self):
        assert d.parse_duration_input("2:30") == 150
        assert d.parse_duration_input("5:00") == 300

    def test_parses_minutes_seconds_dict(self):
        assert d.parse_duration_input({"minutes": 2, "seconds": 30}) == 150

    def test_parse_rejects_garbage(self):
        for bad in ("abc", "2:99:99", None, {"minutes": "x"}, "2:xx"):
            with pytest.raises(d.DurationError):
                d.parse_duration_input(bad)

    def test_parse_enforces_range(self):
        with pytest.raises(d.DurationError):
            d.parse_duration_input("6:00")  # 360 > 300


class TestBackwardCompat:
    def test_default_is_explicit(self):
        assert d.DEFAULT_TARGET_SECONDS == 60  # documented intentional default

    def test_infer_uses_explicit_field(self):
        assert d.infer_target_seconds({"target_duration_seconds": 150}) == 150

    def test_infer_falls_back_to_default_when_absent(self):
        assert d.infer_target_seconds({}) == d.DEFAULT_TARGET_SECONDS

    def test_infer_ignores_invalid_stored_value(self):
        # A corrupt legacy value must not crash — fall back to default.
        assert d.infer_target_seconds({"target_duration_seconds": "oops"}) == d.DEFAULT_TARGET_SECONDS

    def test_presets_are_canonical(self):
        # UI presets must map to canonical seconds.
        assert d.PRESETS == (30, 60, 150, 300)
