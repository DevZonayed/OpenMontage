"""Canonical story-duration contract for OpenMontage productions.

A project's desired length is a single canonical field — ``target_duration_seconds``
— a finite integer in **[1, 300]**. Everything downstream derives from it:

  * exact integer frame math (``target_duration_seconds * fps``),
  * a ~150 wpm narration word budget (planning),
  * timeline / composition total-frames,
  * render policy (timeouts / disk estimate) and the final duration gate.

There is NO hidden demo length: the default is an explicit, documented constant.
``target`` (what the user asked for) is always distinct from the *measured* output
duration reported by ffprobe after a render.
"""

from __future__ import annotations

import math
from typing import Any

MIN_TARGET_SECONDS = 1
MAX_TARGET_SECONDS = 300           # current product contract: up to 5 minutes
DEFAULT_TARGET_SECONDS = 60        # intentional, documented default (1:00)
DEFAULT_FPS = 30
WORDS_PER_MINUTE = 150             # spoken-narration planning rate

# UI presets (canonical seconds): 0:30, 1:00, 2:30, 5:00
PRESETS = (30, 60, 150, 300)


class DurationError(ValueError):
    """UI-safe validation error carrying an HTTP status."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def validate_target_seconds(value: Any) -> int:
    """Return a canonical int in [1, 300] or raise DurationError.

    Accepts an int or an *integral* finite float (e.g. ``60.0`` → 60). Rejects
    booleans, NaN/Inf, fractional floats, non-numeric types, and strings.
    """
    # bool is a subclass of int — refuse it explicitly (True==1 would sneak in).
    if isinstance(value, bool):
        raise DurationError("Duration must be a whole number of seconds.")
    if isinstance(value, int):
        secs = value
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise DurationError("Duration must be a finite number.")
        if not float(value).is_integer():
            raise DurationError("Duration must be a whole number of seconds.")
        secs = int(value)
    else:
        raise DurationError("Duration must be a whole number of seconds.")
    if secs < MIN_TARGET_SECONDS or secs > MAX_TARGET_SECONDS:
        raise DurationError(
            f"Duration must be between {MIN_TARGET_SECONDS} and {MAX_TARGET_SECONDS} seconds "
            f"(up to {format_mmss(MAX_TARGET_SECONDS)}).")
    return secs


def frames_for(seconds: Any, fps: int = DEFAULT_FPS) -> int:
    """Exact integer frame count for a validated duration."""
    secs = validate_target_seconds(seconds)
    fps = int(fps)
    if fps <= 0:
        raise DurationError("fps must be a positive integer.")
    return secs * fps


def word_budget(seconds: Any, wpm: int = WORDS_PER_MINUTE) -> int:
    """Planned narration word budget at ~``wpm`` words per minute."""
    secs = validate_target_seconds(seconds)
    return round(secs / 60.0 * wpm)


def format_mmss(seconds: Any) -> str:
    """Canonical ``M:SS`` display (no leading zero on minutes)."""
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def parse_duration_input(value: Any) -> int:
    """Parse a UI/API duration input into a canonical, validated int.

    Accepts: an int/integral-float; an ``"M:SS"`` string; or a
    ``{"minutes": m, "seconds": s}`` mapping. Everything else raises.
    """
    if isinstance(value, bool):
        raise DurationError("Duration must be a whole number of seconds.")
    if isinstance(value, (int, float)):
        return validate_target_seconds(value)
    if isinstance(value, str):
        text = value.strip()
        if ":" in text:
            parts = text.split(":")
            if len(parts) != 2 or not all(p.isdigit() for p in parts):
                raise DurationError("Enter the duration as M:SS (e.g. 2:30).")
            mm, ss = int(parts[0]), int(parts[1])
            if ss >= 60:
                raise DurationError("Seconds must be 0–59.")
            return validate_target_seconds(mm * 60 + ss)
        if text.isdigit():
            return validate_target_seconds(int(text))
        raise DurationError("Enter a duration in seconds or as M:SS.")
    if isinstance(value, dict):
        try:
            mm = int(value.get("minutes", 0))
            ss = int(value.get("seconds", 0))
        except (TypeError, ValueError):
            raise DurationError("Minutes and seconds must be whole numbers.")
        if ss < 0 or ss >= 60 or mm < 0:
            raise DurationError("Seconds must be 0–59.")
        return validate_target_seconds(mm * 60 + ss)
    raise DurationError("Enter a duration in seconds or as M:SS.")


def infer_target_seconds(intake: Any, *, default: int = DEFAULT_TARGET_SECONDS) -> int:
    """Backward-compatible read: use a valid stored ``target_duration_seconds`` or
    fall back to the documented default — never a destructive rewrite, never a
    crash on a corrupt legacy value."""
    if isinstance(intake, dict):
        raw = intake.get("target_duration_seconds")
        if raw is not None:
            try:
                return validate_target_seconds(raw)
            except DurationError:
                return default
    return default
