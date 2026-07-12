"""Scoped Claude-Code launcher for the Z.AI (GLM) coding plan.

Run as ``python -m lib.zai_launcher`` (optionally with args forwarded to Claude
Code). It reads the Z.AI key from the OS keychain **at launch time**, sets ONLY
the child process environment (``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_AUTH_TOKEN``),
and execs Claude Code. It never:

  * prints or logs the token,
  * writes the token to any file,
  * mutates the user's global ``~/.claude/settings.json`` or their Claude Max OAuth.

This is the isolated execution path the UI/AGENT_GUIDE point at when Z.AI is the
selected engine. Because the launcher itself reads the key, the token is never in
any argv or Terminal command — only ``python -m lib.zai_launcher`` is.
"""

from __future__ import annotations

import os
import shutil
import sys


def main(argv: list[str] | None = None) -> int:
    from lib.zai_credentials import ZaiCredentialError, build_scoped_env

    args = list(sys.argv[1:] if argv is None else argv)
    try:
        env = build_scoped_env()  # reads key from keychain; sets scoped vars
    except ZaiCredentialError as exc:
        # Never includes the key.
        sys.stderr.write(f"Cannot launch: {exc}\n")
        return 2

    claude = shutil.which("claude")
    if not claude:
        sys.stderr.write("Cannot launch: the 'claude' (Claude Code) CLI is not on PATH.\n")
        return 3

    # Replace this process with Claude Code, carrying ONLY the scoped env. The
    # token lives in this process's env, never in argv.
    try:
        os.execve(claude, ["claude", *args], env)
    except OSError as exc:  # pragma: no cover - exec failure is environment-specific
        sys.stderr.write(f"Cannot launch Claude Code: {type(exc).__name__}\n")
        return 4
    return 0  # unreachable if execve succeeds


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
