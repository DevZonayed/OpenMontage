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
  forceFixtures?: boolean;
  fixtureFallback?: boolean;
}

export function mount(container: HTMLElement, options: MountOptions): () => void {
  const client = new BacklotClient({
    projectId: options.projectId,
    baseUrl: options.baseUrl,
    forceFixtures: options.forceFixtures,
    fixtureFallback: options.fixtureFallback ?? true,
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
