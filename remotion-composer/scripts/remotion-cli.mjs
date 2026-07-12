// Safe, pinned Remotion CLI runner.
//
// The ONLY sanctioned way to invoke the Remotion CLI from this package. It:
//   1. resolves the ABSOLUTE project-local binary `node_modules/.bin/remotion`,
//   2. verifies that path is contained inside this worktree,
//   3. verifies it is an existing, executable regular file,
//   4. spawns it directly — NO shell, NO `npx`, NO reliance on PATH.
//
// This forecloses the entire unsafe-invocation class: `npx remotion` (network /
// package drift), a bare `remotion` from PATH (wrong binary), and using
// `remotion --version` as a readiness probe. If the binary is missing or not
// executable, this fails CLOSED with a non-zero exit and a clear message.
//
// Pure helpers are exported for unit testing; the CLI entry runs when executed.

import { spawnSync } from "node:child_process";
import { accessSync, constants, statSync } from "node:fs";
import { dirname, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

export class RemotionCliError extends Error {}

/** Absolute path to this package root (the folder containing package.json). */
export function projectRoot() {
  return resolve(dirname(fileURLToPath(import.meta.url)), "..");
}

/**
 * Resolve and validate the pinned local Remotion binary.
 * Throws RemotionCliError (fail-closed) if it is outside the root, missing,
 * not a regular file, or not executable.
 */
export function resolveRemotionBin(root = projectRoot()) {
  const rootAbs = resolve(root);
  const bin = resolve(rootAbs, "node_modules", ".bin", "remotion");

  // Containment: the resolved binary MUST live inside the worktree/package root.
  const rootWithSep = rootAbs.endsWith(sep) ? rootAbs : rootAbs + sep;
  if (!bin.startsWith(rootWithSep)) {
    throw new RemotionCliError(`Refusing to run a Remotion binary outside the project root: ${bin}`);
  }

  let st;
  try {
    st = statSync(bin);
  } catch {
    throw new RemotionCliError(
      `Local Remotion binary not found at ${bin}. Run \`npm ci\` in remotion-composer/ first.`,
    );
  }
  if (!st.isFile()) {
    throw new RemotionCliError(`Local Remotion path is not a regular file: ${bin}`);
  }
  try {
    accessSync(bin, constants.X_OK);
  } catch {
    throw new RemotionCliError(`Local Remotion binary is not executable: ${bin}`);
  }
  return bin;
}

/**
 * Run the pinned Remotion CLI, forwarding `args` verbatim. Never uses a shell or
 * npx. `spawnImpl` is injectable for tests. Returns the child exit code.
 */
export function runRemotion(args, { root, spawnImpl = spawnSync } = {}) {
  if (!Array.isArray(args)) {
    throw new RemotionCliError("args must be an array of strings");
  }
  const bin = resolveRemotionBin(root);
  const res = spawnImpl(bin, args, {
    stdio: "inherit",
    shell: false, // never interpret through a shell
    cwd: resolve(root ?? projectRoot()),
  });
  if (res.error) throw new RemotionCliError(`Failed to spawn Remotion: ${res.error.message}`);
  return typeof res.status === "number" ? res.status : 1;
}

// CLI entry — only when executed directly (not when imported by a test).
const isMain = (() => {
  try {
    return process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
  } catch {
    return false;
  }
})();

if (isMain) {
  try {
    process.exit(runRemotion(process.argv.slice(2)));
  } catch (e) {
    console.error(String(e instanceof Error ? e.message : e));
    process.exit(1);
  }
}
