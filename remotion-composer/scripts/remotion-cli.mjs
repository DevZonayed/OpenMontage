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
import { accessSync, constants, realpathSync, statSync } from "node:fs";
import { dirname, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

export class RemotionCliError extends Error {}

/** Absolute path to this package root (the folder containing package.json). */
export function projectRoot() {
  return resolve(dirname(fileURLToPath(import.meta.url)), "..");
}

/**
 * Resolve and validate the pinned local Remotion binary.
 *
 * `.bin/remotion` is a SYMLINK, so we follow it with `realpathSync` and verify the
 * REAL target — not just the textual link path — is contained inside the (also
 * realpath-resolved) worktree root. A link pointing outside the tree therefore
 * fails closed instead of executing external code. Returns the verified real path.
 *
 * Throws RemotionCliError (fail-closed) if the target is missing, outside the
 * root, not a regular file, or not executable.
 */
export function resolveRemotionBin(root = projectRoot()) {
  // Canonicalize the root (worktrees can contain symlinked path components).
  let rootReal;
  try {
    rootReal = realpathSync(resolve(root));
  } catch {
    rootReal = resolve(root); // non-existent root → link resolution below fails closed
  }
  const linkPath = resolve(rootReal, "node_modules", ".bin", "remotion");

  // Follow the symlink to its REAL absolute target.
  let real;
  try {
    real = realpathSync(linkPath);
  } catch {
    throw new RemotionCliError(
      `Local Remotion binary not found at ${linkPath}. Run \`npm ci\` in remotion-composer/ first.`,
    );
  }

  // Containment: the REAL target must live inside the worktree/package root.
  const rootWithSep = rootReal.endsWith(sep) ? rootReal : rootReal + sep;
  if (!real.startsWith(rootWithSep)) {
    throw new RemotionCliError(
      `Refusing to run a Remotion binary whose real path is outside the project root: ${real}`,
    );
  }

  const st = statSync(real);
  if (!st.isFile()) {
    throw new RemotionCliError(`Local Remotion real path is not a regular file: ${real}`);
  }
  try {
    accessSync(real, constants.X_OK);
  } catch {
    throw new RemotionCliError(`Local Remotion binary is not executable: ${real}`);
  }
  return real;
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
