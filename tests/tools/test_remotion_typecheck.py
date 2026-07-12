"""Compile contract: the Remotion composer package must typecheck with ZERO errors.

Remotion v4's `<Composition component={...}>` requires prop types assignable to
`Record<string, unknown>`; a TypeScript `interface` is NOT (no implicit index
signature) while a `type` alias IS. This gate fails RED if any composition
regresses to an `interface` prop type (or any other `tsc --noEmit` error) so the
full-Remotion architecture keeps a clean type baseline.

Enforcement: in CI (`CI=1`, set automatically by GitHub Actions) or when
`OPENMONTAGE_ENFORCE_REMOTION_TYPECHECK=1`, a MISSING toolchain FAILS with an
actionable install message instead of silently skipping — so the gate can never
be a no-op on a fresh checkout. Locally (unenforced) it skips when the composer
deps aren't installed. CI installs them via `make remotion-install` (see ci.yml).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
_COMPOSER = _REPO / "remotion-composer"
_TSC = _COMPOSER / "node_modules" / ".bin" / "tsc"

_ENFORCED = bool(
    os.environ.get("CI") or os.environ.get("OPENMONTAGE_ENFORCE_REMOTION_TYPECHECK")
)


def _missing_toolchain_action(enforced: bool) -> tuple[str, str]:
    """Decide what to do when the tsc toolchain is absent.

    Returns ("fail"|"skip", actionable_message). In enforced environments a
    missing toolchain is a hard failure so the zero-error gate is never skipped.
    """
    msg = (
        f"remotion-composer TypeScript toolchain missing ({_TSC}). "
        "Run `make remotion-install` (npm ci) before the test suite so the "
        "zero-error Remotion typecheck gate is enforced."
    )
    return ("fail", msg) if enforced else ("skip", msg)


def test_missing_toolchain_fails_in_enforced_env():
    action, msg = _missing_toolchain_action(True)
    assert action == "fail"
    assert "make remotion-install" in msg


def test_missing_toolchain_skips_when_unenforced():
    action, _ = _missing_toolchain_action(False)
    assert action == "skip"


def test_remotion_composer_typechecks_clean():
    if not _TSC.exists():
        action, msg = _missing_toolchain_action(_ENFORCED)
        if action == "fail":
            pytest.fail(msg)
        pytest.skip(msg)

    proc = subprocess.run(
        [str(_TSC), "--noEmit"],
        cwd=str(_COMPOSER),
        capture_output=True,
        text=True,
        timeout=300,
    )
    # tsc prints one line per error to stdout; a clean run exits 0 with no output.
    errors = [ln for ln in proc.stdout.splitlines() if "error TS" in ln]
    assert proc.returncode == 0 and not errors, (
        f"remotion-composer tsc --noEmit reported {len(errors)} error(s):\n"
        + "\n".join(errors[:40])
    )
