// Single media-resolution seam shared by the Remotion Player AND the pinned CLI
// render, so a saved project-local `source` resolves to the SAME loadable URL in
// both — never "real in preview, placeholder in the final render". Path safety is
// preserved: absolute http/https/blob pass through, everything else is confined to
// the project's `/media/{id}/...` space and traversal is rejected.

export interface AssetResolveOptions {
  projectId?: string | null;
  assetBaseUrl?: string | null; // e.g. the page origin, or http://127.0.0.1:<port> during a render
}

/**
 * Resolve a stored composition `source` to a browser-loadable URL, or `null` when
 * it cannot be safely resolved (→ the composition renders a designed placeholder).
 * Deterministic: identical inputs always produce the identical URL, which is what
 * makes Player and CLI agree.
 */
export function resolveAssetSrc(
  source: string | null | undefined,
  opts: AssetResolveOptions = {},
): string | null {
  if (!source || typeof source !== "string") return null;
  const s = source.trim();
  if (s === "" || s.includes("\\")) return null;
  // Already-loadable absolute URLs (or protocol-relative / blob) pass through.
  if (/^(https?:)?\/\//.test(s) || s.startsWith("blob:")) return s;
  // Any other explicit scheme (javascript:, file:, data:) is unsafe.
  if (/^[a-z][a-z0-9+.-]*:/i.test(s)) return null;
  // Reject traversal in any path form.
  if (s.split("/").some((p) => p === "..")) return null;

  const base = (opts.assetBaseUrl || "").replace(/\/+$/, "");
  // Same-origin absolute path (already `/media/...` or `/thumb/...`): prefix base only.
  if (s.startsWith("/")) {
    if (s.startsWith("//")) return null; // protocol-relative handled above; bare `//` is unsafe here
    return base + s;
  }
  // Project-local relative path → /media/{projectId}/{path}. Needs a project id.
  const pid = opts.projectId;
  if (!pid) return null;
  const encoded = s.split("/").filter(Boolean).map(encodeURIComponent).join("/");
  return `${base}/media/${encodeURIComponent(pid)}/${encoded}`;
}

/** True when a source resolves to something the browser can actually fetch. */
export function isResolvableMedia(
  source: string | null | undefined,
  opts: AssetResolveOptions = {},
): boolean {
  return resolveAssetSrc(source, opts) !== null;
}
