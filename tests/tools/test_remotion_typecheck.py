"""Compile contract: the Remotion composer package must typecheck with ZERO errors.

Remotion v4's `<Composition component={...}>` requires prop types assignable to
`Record<string, unknown>`; a TypeScript `interface` is NOT (no implicit index
signature) while a `type` alias IS. This gate fails RED if any composition
regresses to an `interface` prop type (or any other `tsc --noEmit` error) so the
full-Remotion architecture keeps a clean type baseline.

Skipped only when the toolchain is genuinely absent (no node_modules / tsc) — it
never silently passes on a real type error.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
_COMPOSER = _REPO / "remotion-composer"
_TSC = _COMPOSER / "node_modules" / ".bin" / "tsc"


@pytest.mark.skipif(not _TSC.exists(), reason="remotion-composer/node_modules tsc not installed")
def test_remotion_composer_typechecks_clean():
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
