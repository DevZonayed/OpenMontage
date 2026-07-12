// Bundle entry for the embedded Backlot Remotion studio.
//
// Built by scripts/build-studio.mjs into backlot/ui/studio.bundle.js as an IIFE
// that exposes `window.BacklotStudio.mount(container, options)`. The static Backlot
// server serves it from /ui/studio.bundle.js — no server change required.

import React from "react";
import { createRoot } from "react-dom/client";
import { StudioApp } from "./StudioApp";
import { BacklotClient } from "../composition/client";

export interface MountOptions {
  projectId: string;
  baseUrl?: string;
  forceFixtures?: boolean; // EXPLICIT demo mode ("Try demo mode")
}

export function mount(container: HTMLElement, options: MountOptions): () => void {
  // Demo mode is EXPLICIT only: the caller opts in, or the URL carries ?demo=1.
  // There is no automatic fixture fallback — a failed live fetch shows a
  // reconnecting indicator, never fabricated data.
  const urlDemo =
    typeof location !== "undefined" && /[?&]demo=1\b/.test(location.search);
  const client = new BacklotClient({
    projectId: options.projectId,
    baseUrl: options.baseUrl,
    forceFixtures: options.forceFixtures ?? urlDemo,
  });
  const root = createRoot(container);
  root.render(
    <React.StrictMode>
      <StudioApp client={client} />
    </React.StrictMode>,
  );
  return () => root.unmount();
}

// Expose on window for the vanilla editor.html bootstrap.
declare global {
  interface Window {
    BacklotStudio?: { mount: typeof mount };
  }
}
if (typeof window !== "undefined") {
  window.BacklotStudio = { mount };
}
