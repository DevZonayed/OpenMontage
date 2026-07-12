// Builds the embedded Backlot Remotion studio into a single IIFE bundle that the
// static Backlot server serves from /ui/studio.bundle.js. The Backlot UI has no
// build pipeline, so we commit the built artifact and regenerate it with:
//   npm run build:studio
//
// The bundle is intentionally lean: it pulls in @remotion/player + the canonical
// TimelineFrame composition + the composition model — NOT Root.tsx / the heavy
// map/font compositions.

import { build } from "esbuild";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..");
const outfile = resolve(root, "..", "backlot", "ui", "studio.bundle.js");

const result = await build({
  entryPoints: [resolve(root, "src", "studio", "mount.tsx")],
  outfile,
  bundle: true,
  format: "iife",
  platform: "browser",
  target: ["es2020"],
  jsx: "automatic",
  minify: true,
  sourcemap: false,
  legalComments: "none",
  define: {
    "process.env.NODE_ENV": '"production"',
    "process.env.REMOTION_ROOT": '""',
  },
  loader: { ".tsx": "tsx", ".ts": "ts" },
  logLevel: "info",
  metafile: true,
});

const bytes = Object.values(result.metafile.outputs)[0]?.bytes ?? 0;
console.log(`studio.bundle.js → ${(bytes / 1024).toFixed(0)} KB at ${outfile}`);
