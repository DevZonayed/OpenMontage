// Static + behavioural contract for the pinned Remotion CLI runner.
// Guarantees the unsafe-invocation class stays foreclosed.

import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import {
  RemotionCliError,
  resolveRemotionBin,
  runRemotion,
  SpawnImpl,
  SpawnResult,
} from "../../scripts/remotion-cli.mjs";

const here = resolve(fileURLToPath(import.meta.url), "..");
const pkgRoot = resolve(here, "..", "..");

describe("package.json invocation contract", () => {
  const pkg = JSON.parse(readFileSync(resolve(pkgRoot, "package.json"), "utf-8")) as {
    scripts: Record<string, string>;
  };
  const scripts = Object.entries(pkg.scripts);

  it("no script uses npx", () => {
    for (const [name, cmd] of scripts) {
      expect(cmd, `script "${name}"`).not.toMatch(/\bnpx\b/);
    }
  });

  it("no script invokes a bare PATH `remotion` (must go through the wrapper)", () => {
    for (const [name, cmd] of scripts) {
      // Allow "node scripts/remotion-cli.mjs ..."; forbid a leading/`&&`-chained bare `remotion`.
      const bareRemotion = /(^|&&|\||;)\s*remotion\b/;
      expect(bareRemotion.test(cmd), `script "${name}": ${cmd}`).toBe(false);
    }
  });

  it("no script uses `remotion --version` as a readiness probe", () => {
    for (const [, cmd] of scripts) {
      expect(cmd).not.toMatch(/remotion[^\n]*--version/);
    }
  });

  it("dropped the unsafe `upgrade` script entirely", () => {
    expect(pkg.scripts.upgrade).toBeUndefined();
  });

  it("remotion-touching scripts route through scripts/remotion-cli.mjs", () => {
    for (const [name, cmd] of scripts) {
      if (name === "start" || name === "build" || name === "remotion") {
        expect(cmd).toContain("scripts/remotion-cli.mjs");
        expect(cmd.startsWith("node ")).toBe(true);
      }
    }
  });
});

describe("resolveRemotionBin", () => {
  it("resolves an absolute, project-contained node_modules/.bin/remotion", () => {
    const bin = resolveRemotionBin(pkgRoot);
    expect(bin.startsWith(pkgRoot + sep)).toBe(true);
    expect(bin.endsWith(["node_modules", ".bin", "remotion"].join(sep))).toBe(true);
  });

  it("fails closed when the binary is missing (wrong root)", () => {
    expect(() => resolveRemotionBin("/tmp/definitely-not-a-remotion-project-xyz")).toThrow(
      RemotionCliError,
    );
  });
});

describe("runRemotion", () => {
  it("forwards args verbatim to the absolute local bin, with no shell", () => {
    let captured: { bin: string; args: string[]; opts: Record<string, unknown> } | null = null;
    const fakeSpawn: SpawnImpl = (bin, args, opts): SpawnResult => {
      captured = { bin, args, opts: opts as Record<string, unknown> };
      return { status: 0 };
    };
    const code = runRemotion(["render", "src/index.tsx", "TimelineFrame", "out.mp4", "--frames=0-9"], {
      root: pkgRoot,
      spawnImpl: fakeSpawn,
    });
    expect(code).toBe(0);
    expect(captured).not.toBeNull();
    const cap = captured!;
    expect(cap.bin.startsWith(pkgRoot + sep)).toBe(true);
    expect(cap.bin.endsWith(["node_modules", ".bin", "remotion"].join(sep))).toBe(true);
    expect(cap.bin).not.toMatch(/npx/);
    expect(cap.args).toEqual(["render", "src/index.tsx", "TimelineFrame", "out.mp4", "--frames=0-9"]);
    expect(cap.opts.shell).toBe(false);
  });

  it("propagates the child's non-zero exit code", () => {
    const fakeSpawn: SpawnImpl = () => ({ status: 3 });
    expect(runRemotion(["render"], { root: pkgRoot, spawnImpl: fakeSpawn })).toBe(3);
  });

  it("fails closed (throws) when the local bin cannot be resolved", () => {
    const fakeSpawn: SpawnImpl = () => ({ status: 0 });
    expect(() =>
      runRemotion(["render"], { root: "/tmp/no-remotion-here-xyz", spawnImpl: fakeSpawn }),
    ).toThrow(RemotionCliError);
  });

  it("rejects non-array args", () => {
    // @ts-expect-error intentional misuse
    expect(() => runRemotion("render", { root: pkgRoot })).toThrow(RemotionCliError);
  });
});
