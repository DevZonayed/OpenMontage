import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Player, PlayerRef } from "@remotion/player";
import { TimelineFrame, TimelineFrameProps } from "../TimelineComposition";
import {
  CanonicalComposition,
  CompositionAsset,
  Layer,
  LayerType,
  makeId,
  trackKindForType,
} from "../composition/model";
import {
  backendPayloadToCanonical,
  canonicalToBackendDoc,
  renderProps,
} from "../composition/adapter";
import { validateComposition } from "../composition/validate";
import { History } from "../composition/history";
import {
  addLayer,
  moveLayer,
  muteTrack,
  removeLayer,
  resizeLayer,
  revertAsset,
  setAssetApproval,
  setLayerText,
  setVolume,
  splitLayer,
  trimLayer,
} from "../composition/operations";
import { BacklotClient } from "../composition/client";
import { ProjectOverview } from "../composition/status";
import { PreferencesPanel } from "./PreferencesPanel";

// ── helpers ──
function timecode(frame: number, fps: number): string {
  const totalSeconds = frame / fps;
  const m = Math.floor(totalSeconds / 60);
  const s = Math.floor(totalSeconds % 60);
  const f = Math.round(frame % fps);
  return `${m}:${String(s).padStart(2, "0")}.${String(f).padStart(2, "0")}`;
}

const TYPE_COLOR: Record<string, string> = {
  video: "#6ea8fe",
  image: "#63d2a4",
  shape: "#b48ef0",
  text: "#e8c07d",
  caption: "#e8a0c0",
  narration: "#f0a868",
  music: "#8ad0e0",
  sfx: "#d0d060",
};

export interface StudioAppProps {
  client: BacklotClient;
}

export const StudioApp: React.FC<StudioAppProps> = ({ client }) => {
  const histRef = useRef<History<CanonicalComposition> | null>(null);
  const [, force] = useState(0);
  const rerender = useCallback(() => force((n) => n + 1), []);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [frame, setFrame] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [previewZoom, setPreviewZoom] = useState(1);
  const [tlZoom, setTlZoom] = useState(0.25); // px per frame
  const [etag, setEtag] = useState<string | undefined>(undefined);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [usedFixture, setUsedFixture] = useState(false);
  const [renderReady, setRenderReady] = useState(false);
  const [renderReason, setRenderReason] = useState("");
  const [rendering, setRendering] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [rightTab, setRightTab] = useState<"inspector" | "style">("inspector");

  // Read-only project overview: a truthful duration + a short guidance line for
  // the workspace strip. It is NOT an automation controller — the Studio is the
  // editor. A failure here never breaks editing.
  const [overview, setOverview] = useState<ProjectOverview | null>(null);

  const playerRef = useRef<PlayerRef>(null);

  const model = histRef.current?.present ?? null;

  // ── load ──
  const loadOverview = useCallback(async () => {
    try {
      setOverview(await client.getStatus());
    } catch {
      // Manual-first: the overview is advisory. Keep the last known value and
      // never block the editor on it.
    }
  }, [client]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const payload = await client.getTimeline();
      const c = backendPayloadToCanonical(payload, { id: client.projectId });
      histRef.current = new History(c, 120);
      setEtag(payload.etag);
      setRenderReady(payload.remotion_render_ready);
      setRenderReason(payload.remotion_reason);
      setUsedFixture(client.usedFixture);
      setDirty(false);
      setSelected(c.layers[0]?.id ?? null);
      rerender();
      void loadOverview();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [client, rerender, loadOverview]);

  useEffect(() => {
    void load();
  }, [load]);

  // ── player frame tracking ──
  useEffect(() => {
    const p = playerRef.current;
    if (!p) return;
    const onFrame = (e: { detail: { frame: number } }) => setFrame(e.detail.frame);
    const onPlay = () => setPlaying(true);
    const onPause = () => setPlaying(false);
    p.addEventListener("frameupdate", onFrame);
    p.addEventListener("play", onPlay);
    p.addEventListener("pause", onPause);
    return () => {
      p.removeEventListener("frameupdate", onFrame);
      p.removeEventListener("play", onPlay);
      p.removeEventListener("pause", onPause);
    };
    // Re-bind whenever the player instance changes (after load).
  }, [loading]);

  // ── edit application ──
  const apply = useCallback(
    (fn: (c: CanonicalComposition) => CanonicalComposition) => {
      const h = histRef.current;
      if (!h) return;
      try {
        const next = fn(h.present);
        h.commit(next);
        setDirty(true);
        rerender();
      } catch (e) {
        setNotice(e instanceof Error ? e.message : String(e));
      }
    },
    [rerender],
  );

  const undo = useCallback(() => {
    histRef.current?.undo();
    setDirty(true);
    rerender();
  }, [rerender]);
  const redo = useCallback(() => {
    histRef.current?.redo();
    setDirty(true);
    rerender();
  }, [rerender]);

  const save = useCallback(async () => {
    const h = histRef.current;
    if (!h) return;
    const v = validateComposition(h.present);
    if (!v.ok) {
      setNotice(`Cannot save — ${v.errors[0]?.message ?? "invalid composition"}`);
      return;
    }
    setSaving(true);
    setNotice(null);
    try {
      const res = await client.saveTimeline(canonicalToBackendDoc(h.present), etag);
      setEtag(res.etag);
      setDirty(false);
      setNotice("Saved.");
      void loadOverview();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setNotice(msg.includes("changed") ? `Conflict: ${msg}. Reload to merge.` : `Save failed: ${msg}`);
    } finally {
      setSaving(false);
    }
  }, [client, etag, loadOverview]);

  const renderFinal = useCallback(async () => {
    const layerCount = histRef.current?.present.layers.length ?? 0;
    if (layerCount === 0) {
      setNotice("Nothing to render yet — add a first scene to the timeline. A blank render is disabled on purpose.");
      return;
    }
    if (dirty) {
      setNotice("Save your edits before rendering so preview and render match.");
      return;
    }
    setRendering(true);
    setNotice("Rendering with the pinned Remotion CLI…");
    try {
      const r = await client.renderTimeline();
      if (r.ok) {
        setNotice(
          `Rendered ${r.frames_rendered ?? "?"} frames${r.truncated ? " (preview length)" : ""} → ${r.url ?? "output"}`,
        );
      } else {
        setNotice(`Render failed: ${r.reason ?? "unknown"}`);
      }
    } catch (e) {
      setNotice(`Render failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setRendering(false);
    }
  }, [client, dirty]);

  const target = overview?.target ?? null;

  // ── transport ──
  // Clamp to the EFFECTIVE duration (real layers → the composition; empty → the
  // truthful target frames when known, otherwise a safe internal count that is
  // NEVER shown as the user's duration). Always update `frame` so the scrubber
  // works even when there is no Player yet (empty timeline shows the placeholder).
  const seek = useCallback((f: number) => {
    if (!model) return;
    const effectiveFrames = model.layers.length > 0
      ? model.totalFrames
      : (target?.frames || model.totalFrames);
    const clamped = Math.max(0, Math.min(effectiveFrames - 1, Math.round(f)));
    playerRef.current?.seekTo(clamped);   // seek only if a Player exists
    setFrame(clamped);                     // but always move the playhead/timecode
  }, [model, target]);

  const togglePlay = useCallback(() => playerRef.current?.toggle(), []);
  const step = useCallback((delta: number) => seek(frame + delta), [frame, seek]);

  const addFirstScene = useCallback(() => {
    if (!model) return;
    const visibleFrame = addNewLayer(model, apply, setSelected);
    // The Player only mounts once a layer exists; defer the seek two frames so the
    // just-created title lands on a frame where its entrance is fully visible
    // (creation never looks blank). The final render is untouched (starts at 0).
    const raf = typeof window !== "undefined" ? window.requestAnimationFrame : null;
    const run = () => {
      playerRef.current?.seekTo(visibleFrame);
      setFrame(visibleFrame);
    };
    if (raf) raf(() => raf(run));
    else setTimeout(run, 32);
  }, [model, apply]);

  // ── keyboard access ──
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "z") {
        e.preventDefault();
        if (e.shiftKey) redo();
        else undo();
        return;
      }
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        void save();
        return;
      }
      switch (e.key) {
        case " ":
          e.preventDefault();
          togglePlay();
          break;
        case "ArrowLeft":
          e.preventDefault();
          step(e.shiftKey ? -30 : -1);
          break;
        case "ArrowRight":
          e.preventDefault();
          step(e.shiftKey ? 30 : 1);
          break;
        case "Home":
          seek(0);
          break;
        case "End":
          // seek() clamps to the effective end (target frames on an empty timeline).
          if (model) seek(Number.MAX_SAFE_INTEGER);
          break;
        default:
          break;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [redo, save, seek, step, togglePlay, undo, model]);

  // Feed the Player the SAME assetBaseUrl + projectId the CLI would use, so a
  // project-local `source` resolves to the identical media URL in preview + render.
  const assetBaseUrl = typeof window !== "undefined" ? window.location.origin : "";
  const inputProps: TimelineFrameProps | null = useMemo(
    () =>
      model
        ? (renderProps(model, { assetBaseUrl, projectId: client.projectId }) as TimelineFrameProps)
        : null,
    [model, assetBaseUrl, client.projectId],
  );

  if (loading) {
    return <Centered>Loading studio…</Centered>;
  }
  if (error || !model || !inputProps) {
    return (
      <Centered>
        <div>
          <div style={{ color: "#e5544b", marginBottom: 8 }}>Could not load the composition.</div>
          <div style={{ color: "#a0a0a9", fontSize: 13 }}>{error}</div>
          <button style={btn} onClick={() => void load()}>
            Retry
          </button>
        </div>
      </Centered>
    );
  }

  const validation = validateComposition(model);
  const selectedLayer = model.layers.find((l) => l.id === selected) ?? null;
  const hasLayers = model.layers.length > 0;

  // TRUTHFUL DURATION. Never surface the composer's internal minimum (e.g.
  // 1800 frames / 1:00) as the user's duration. A real timeline is authoritative;
  // an empty timeline shows the truthful target, or the pending guidance label.
  let durationText: string;
  if (hasLayers) {
    durationText = `${model.totalFrames} frames · ${model.meta.targetFormatted || `${model.targetDurationSeconds}s`}`;
  } else if (target?.available) {
    durationText = target.label;
  } else {
    durationText = "Duration set after first scene";
  }

  // Scrubber denominator: authoritative frames with layers; truthful target
  // frames when empty; a safe internal count only as a last resort (never shown
  // in the readout when the duration is still pending).
  const displayTotalFrames = hasLayers
    ? model.totalFrames
    : (target?.frames || model.totalFrames);

  const scrubReadout = hasLayers
    ? `${timecode(frame, model.fps)} · f${frame}/${model.totalFrames}`
    : target?.available
      ? `f${frame}/${target.frames}`
      : "Duration set after first scene";

  const renderable = hasLayers;
  const renderDisabledReason = overview?.render?.reason
    ?? "Add a first scene to the timeline to enable rendering.";

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", color: "#ececef" }}>
      {/* COMPACT WORKSPACE-GUIDANCE STRIP — title · short guidance · truthful
          duration. No connect / start / stop / agent controls: this is a manual
          editor, not a command center. */}
      <div style={workspaceStrip} data-testid="workspace-strip">
        <div style={{ display: "flex", alignItems: "baseline", gap: 10, minWidth: 0 }}>
          <strong style={{ fontSize: 14 }}>{overview?.title || model.meta.title || client.projectId}</strong>
          <span style={{ fontSize: 12, color: "#a0a0a9", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} data-testid="workspace-guidance">
            {/* Derived from the LIVE model — never the stale server overview. Once a
                layer exists we never claim "no timeline". */}
            {hasLayers
              ? `${model.layers.length} scene${model.layers.length === 1 ? "" : "s"} on the timeline · edit, preview, and render`
              : (overview?.guidance || "Add your first scene to start editing.")}
          </span>
        </div>
        <span style={pill} data-testid="workspace-duration">{durationText}</span>
      </div>

      {/* Header / actions */}
      <div style={header}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span style={pill} data-testid="tl-meta">
            {model.width}×{model.height} · {model.fps}fps · {durationText}
          </span>
          {usedFixture ? <span style={{ ...pill, borderColor: "#e8c07d", color: "#e8c07d" }}>◐ demo data</span> : null}
          <span
            style={{ ...pill, color: renderReady ? "#4fc283" : "#e8c07d", borderColor: renderReady ? "#4fc283" : "#e8c07d" }}
            title={renderReason}
          >
            {renderReady ? "Remotion ready" : "Remotion not ready"}
          </span>
        </div>
        {/* DOMAIN · RENDERER — save + final render controls. */}
        <div style={{ display: "flex", gap: 8, alignItems: "center" }} data-testid="domain-renderer">
          <span style={domainTag}>Renderer</span>
          <button style={btn} onClick={undo} disabled={!histRef.current?.canUndo} aria-label="Undo">
            Undo
          </button>
          <button style={btn} onClick={redo} disabled={!histRef.current?.canRedo} aria-label="Redo">
            Redo
          </button>
          <button style={{ ...btnPrimary, opacity: dirty ? 1 : 0.6 }} onClick={() => void save()} disabled={saving}>
            {saving ? "Saving…" : dirty ? "Save*" : "Saved"}
          </button>
          <button
            style={{ ...btnAccent, opacity: rendering || !renderable ? 0.5 : 1, cursor: renderable ? "pointer" : "not-allowed" }}
            onClick={() => void renderFinal()}
            disabled={rendering || !renderable}
            title={!renderable ? renderDisabledReason : "Render the final film"}
          >
            {rendering ? "Rendering…" : "▶ Render final film"}
          </button>
        </div>
      </div>

      {notice ? <div style={noticeBar}>{notice}</div> : null}

      <div style={{ display: "flex", flex: 1, minHeight: 0 }}>
        {/* DOMAIN · TIMELINE / ASSETS — scenes, tracks, sequences. */}
        <div style={rail} data-testid="domain-timeline">
          <div style={railTitle}>TIMELINE &amp; ASSETS · SCENES · TRACKS</div>
          {model.tracks.map((t) => (
            <div key={t.id} style={{ marginBottom: 12 }}>
              <div style={trackHeader}>
                <span>{t.label}</span>
                <button
                  style={miniBtn}
                  onClick={() => apply((c) => muteTrack(c, t.id, !t.muted))}
                  aria-label={t.muted ? `Unmute ${t.label}` : `Mute ${t.label}`}
                >
                  {t.muted ? "muted" : "on"}
                </button>
              </div>
              {model.layers
                .filter((l) => trackKindForType(l.type) === t.kind)
                .map((l) => (
                  <LayerRow
                    key={l.id}
                    layer={l}
                    selected={l.id === selected}
                    onSelect={() => {
                      setSelected(l.id);
                      seek(l.startFrame);
                    }}
                  />
                ))}
            </div>
          ))}
          {/* The ongoing add control — only once there are layers. On an empty
              timeline the single prominent CTA is "Add first scene" (below). */}
          {hasLayers ? (
            <button
              style={{ ...miniBtn, marginTop: 8 }}
              data-testid="tl-add-layer"
              onClick={addFirstScene}
            >
              + Add layer
            </button>
          ) : null}
        </div>

        {/* DOMAIN · PREVIEW — the @remotion/player Player + transport. */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }} data-testid="domain-preview">
          <div style={domainStrip}>
            <span style={domainTag}>Preview</span>
            <span style={{ fontSize: 11, color: "#5f5f68" }}>preview = render</span>
          </div>
          {!hasLayers ? (
            <div style={stage}>
              <EmptyTimelineCard target={target} usedFixture={usedFixture} onAddFirstScene={addFirstScene} />
            </div>
          ) : (
          <div style={stage}>
            <div
              style={{
                transform: `scale(${previewZoom})`,
                transformOrigin: "center center",
                transition: "transform 120ms ease",
                width: "100%",
                maxWidth: 1100,
              }}
            >
              <Player
                ref={playerRef}
                component={TimelineFrame}
                inputProps={inputProps}
                durationInFrames={Math.max(1, model.totalFrames)}
                compositionWidth={model.width}
                compositionHeight={model.height}
                fps={model.fps}
                style={{ width: "100%", borderRadius: 10, overflow: "hidden", border: "1px solid #232329" }}
                acknowledgeRemotionLicense
                errorFallback={({ error: e }) => (
                  <Centered>Composition error: {e.message}</Centered>
                )}
              />
            </div>
          </div>
          )}

          <Transport
            readout={scrubReadout}
            frame={frame}
            totalFrames={displayTotalFrames}
            playing={playing}
            previewZoom={previewZoom}
            onZoom={setPreviewZoom}
            onToggle={togglePlay}
            onSeek={seek}
            onStep={step}
          />

          <TimelineLanes
            model={model}
            frame={frame}
            pxPerFrame={tlZoom}
            selected={selected}
            onSelect={(id) => {
              setSelected(id);
              const l = model.layers.find((x) => x.id === id);
              if (l) seek(l.startFrame);
            }}
            onSeek={seek}
            onZoom={setTlZoom}
          />
        </div>

        {/* Tabbed right column: Inspector · Style */}
        <div style={inspector}>
          <div style={{ display: "flex", gap: 4, marginBottom: 10 }}>
            {(["inspector", "style"] as const).map((t) => (
              <button
                key={t}
                onClick={() => setRightTab(t)}
                style={{
                  flex: 1,
                  background: rightTab === t ? "#1c1c21" : "transparent",
                  color: rightTab === t ? "#ececef" : "#a0a0a9",
                  border: `1px solid ${rightTab === t ? "#3a3a44" : "#232329"}`,
                  borderRadius: 6,
                  padding: "5px 4px",
                  fontSize: 11,
                  textTransform: "capitalize",
                  cursor: "pointer",
                }}
              >
                {t === "inspector" ? "Inspector" : "Style"}
              </button>
            ))}
          </div>

          {rightTab === "style" ? (
            <div data-testid="domain-style">
              <div style={railTitle}>STYLE · LEARNED PREFERENCES</div>
              <PreferencesPanel client={client} />
            </div>
          ) : (
            <div data-testid="domain-inspector">
              <div style={railTitle}>INSPECTOR · SELECTED LAYER</div>
              {selectedLayer ? (
                <Inspector
                  key={selectedLayer.id}
                  layer={selectedLayer}
                  asset={model.assets.find((a) => a.id === selectedLayer.assetId) ?? null}
                  onEdit={(edit) => apply((c) => trimLayer(c, selectedLayer.id, edit))}
                  onMove={(f) => apply((c) => moveLayer(c, selectedLayer.id, f))}
                  onResize={(d) => apply((c) => resizeLayer(c, selectedLayer.id, d))}
                  onVolume={(v) => apply((c) => setVolume(c, selectedLayer.id, v))}
                  onSplit={() => apply((c) => splitLayer(c, selectedLayer.id, frame).composition)}
                  onRevert={(assetId, v) => apply((c) => revertAsset(c, assetId, v))}
                  onApprove={(assetId, ap) => apply((c) => setAssetApproval(c, assetId, ap))}
                  onDelete={() => {
                    apply((c) => removeLayer(c, selectedLayer.id));
                    setSelected(null);
                  }}
                  onContentChange={(patch) => apply((c) => setLayerText(c, selectedLayer.id, patch))}
                />
              ) : (
                <div style={{ color: "#5f5f68", fontSize: 13 }}>Select a layer to edit it.</div>
              )}
              <div style={{ height: 1, background: "#232329", margin: "12px 0" }} />
              <ValidationPanel result={validation} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

// ── sub-components ──
const LayerRow: React.FC<{ layer: Layer; selected: boolean; onSelect: () => void }> = ({
  layer,
  selected,
  onSelect,
}) => (
  <div
    role="button"
    tabIndex={0}
    onClick={onSelect}
    onKeyDown={(e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onSelect();
      }
    }}
    style={{
      display: "flex",
      alignItems: "center",
      gap: 8,
      padding: "6px 8px",
      borderRadius: 6,
      cursor: "pointer",
      background: selected ? "#1c1c21" : "transparent",
      border: `1px solid ${selected ? "#3a3a44" : "transparent"}`,
      marginBottom: 3,
    }}
  >
    <span style={{ width: 8, height: 8, borderRadius: 2, background: TYPE_COLOR[layer.type] || "#8a93a3" }} />
    <span style={{ fontSize: 12, color: "#ececef", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
      {layer.text || layer.title || layer.id}
    </span>
    <span style={{ fontSize: 10, color: "#5f5f68" }}>{layer.type}</span>
    {!layer.enabled ? <span style={{ fontSize: 10, color: "#e5544b" }}>off</span> : null}
  </div>
);

const Transport: React.FC<{
  readout: string;
  frame: number;
  totalFrames: number;
  playing: boolean;
  previewZoom: number;
  onZoom: (z: number) => void;
  onToggle: () => void;
  onSeek: (f: number) => void;
  onStep: (d: number) => void;
}> = ({ readout, frame, totalFrames, playing, previewZoom, onZoom, onToggle, onSeek, onStep }) => (
  <div style={transport}>
    <button style={btn} onClick={() => onStep(-1)} aria-label="Previous frame">
      ◂
    </button>
    <button style={btnPrimary} onClick={onToggle} aria-label={playing ? "Pause" : "Play"}>
      {playing ? "❚❚" : "►"}
    </button>
    <button style={btn} onClick={() => onStep(1)} aria-label="Next frame">
      ▸
    </button>
    <input
      type="range"
      min={0}
      max={Math.max(0, totalFrames - 1)}
      value={frame}
      onChange={(e) => onSeek(Number(e.target.value))}
      style={{ flex: 1 }}
      aria-label="Scrub"
    />
    <span style={{ fontFamily: "ui-monospace, monospace", fontSize: 12, minWidth: 200, textAlign: "right" }} data-testid="scrub-readout">
      {readout}
    </span>
    <span style={{ fontSize: 11, color: "#a0a0a9" }}>zoom</span>
    <input
      type="range"
      min={0.25}
      max={2}
      step={0.05}
      value={previewZoom}
      onChange={(e) => onZoom(Number(e.target.value))}
      style={{ width: 90 }}
      aria-label="Preview zoom"
    />
    <span style={{ fontFamily: "ui-monospace, monospace", fontSize: 11, minWidth: 40 }}>
      {Math.round(previewZoom * 100)}%
    </span>
  </div>
);

const TimelineLanes: React.FC<{
  model: CanonicalComposition;
  frame: number;
  pxPerFrame: number;
  selected: string | null;
  onSelect: (id: string) => void;
  onSeek: (f: number) => void;
  onZoom: (z: number) => void;
}> = ({ model, frame, pxPerFrame, selected, onSelect, onSeek, onZoom }) => {
  const width = Math.max(200, model.totalFrames * pxPerFrame);
  return (
    <div style={{ borderTop: "1px solid #232329", background: "#0c0c0f", padding: "8px 10px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
        <span style={{ fontSize: 11, color: "#5f5f68", letterSpacing: "0.08em" }}>
          REMOTION SEQUENCES — each block is a &lt;Sequence&gt;
        </span>
        <input
          type="range"
          min={0.05}
          max={1}
          step={0.01}
          value={pxPerFrame}
          onChange={(e) => onZoom(Number(e.target.value))}
          style={{ width: 100 }}
          aria-label="Timeline zoom"
        />
      </div>
      <div style={{ overflowX: "auto", position: "relative" }}>
        <div
          style={{ position: "relative", width, height: 10, marginBottom: 4, cursor: "pointer" }}
          onClick={(e) => {
            const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
            onSeek((e.clientX - rect.left) / pxPerFrame);
          }}
        >
          <div style={{ position: "absolute", left: frame * pxPerFrame, top: 0, bottom: -400, width: 2, background: "#e5544b", zIndex: 5 }} />
        </div>
        {["visual", "text", "audio"].map((kind) => (
          <div key={kind} style={{ position: "relative", width, height: 26, marginBottom: 3 }}>
            {model.layers
              .filter((l) => trackKindForType(l.type) === kind)
              .map((l) => (
                <div
                  key={l.id}
                  role="button"
                  tabIndex={0}
                  onClick={() => onSelect(l.id)}
                  onKeyDown={(e) => (e.key === "Enter" ? onSelect(l.id) : null)}
                  title={`${l.type}:${l.id} — start ${l.startFrame}, ${l.durationFrames}f`}
                  style={{
                    position: "absolute",
                    left: l.startFrame * pxPerFrame,
                    // Keep blocks legible — never a 2-char clipped sliver.
                    width: Math.max(44, l.durationFrames * pxPerFrame),
                    height: 22,
                    top: 2,
                    borderRadius: 4,
                    background: (TYPE_COLOR[l.type] || "#8a93a3") + (l.enabled ? "cc" : "44"),
                    border: `1px solid ${selected === l.id ? "#fff" : "transparent"}`,
                    fontSize: 10,
                    color: "#0a0a0c",
                    padding: "3px 5px",
                    overflow: "hidden",
                    whiteSpace: "nowrap",
                    cursor: "pointer",
                  }}
                >
                  {l.text || l.title || l.id}
                </div>
              ))}
          </div>
        ))}
      </div>
    </div>
  );
};

const Inspector: React.FC<{
  layer: Layer;
  asset: CompositionAsset | null;
  onEdit: (edit: { startFrame?: number; durationFrames?: number }) => void;
  onMove: (f: number) => void;
  onResize: (d: number) => void;
  onVolume: (v: number) => void;
  onSplit: () => void;
  onRevert: (assetId: string, version: number) => void;
  onApprove: (assetId: string, approved: boolean) => void;
  onDelete: () => void;
  onContentChange: (patch: { text?: string; title?: string }) => void;
}> = ({ layer, asset, onEdit, onMove, onResize, onVolume, onSplit, onRevert, onApprove, onDelete, onContentChange }) => {
  const isTextLayer = layer.type === "text" || layer.type === "caption";
  return (
    <div>
      <div style={{ fontSize: 12, color: "#a0a0a9", marginBottom: 8 }}>
        LAYER · {layer.type} · {layer.id}
      </div>
      {isTextLayer && (
        <Field label="Content">
          <textarea
            key={`content-${layer.id}`}
            data-testid="inspector-content"
            defaultValue={layer.title || layer.text || ""}
            placeholder="Scene text (shown on screen)"
            onChange={(e) => onContentChange({ text: e.target.value, title: e.target.value })}
            style={{ ...inp, height: 60, resize: "vertical", fontSize: 14 }}
          />
        </Field>
      )}
      <AssetPanel asset={asset} onRevert={onRevert} onApprove={onApprove} />
      <Field label="Start frame">
        <input
          type="number"
          defaultValue={layer.startFrame}
          onBlur={(e) => onMove(Number(e.target.value))}
          style={inp}
        />
      </Field>
      <Field label="Duration (frames)">
        <input
          type="number"
          defaultValue={layer.durationFrames}
          onBlur={(e) => onResize(Number(e.target.value))}
          style={inp}
        />
      </Field>
      {(layer.type === "narration" || layer.type === "music" || layer.type === "sfx") && (
        <Field label={`Volume ${Math.round((layer.volume ?? 1) * 100)}%`}>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            defaultValue={layer.volume ?? 1}
            onChange={(e) => onVolume(Number(e.target.value))}
            style={{ width: "100%" }}
          />
        </Field>
      )}
      <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
        <button style={miniBtn} onClick={onSplit}>
          Split at playhead
        </button>
        <button style={{ ...miniBtn, color: "#e5544b" }} onClick={onDelete}>
          Delete
        </button>
      </div>
    </div>
  );
};

const STATUS_COLOR: Record<string, string> = {
  placeholder: "#8a93a3",
  generating: "#e8c07d",
  ready: "#6aa1ff",
  approved: "#4fc283",
  failed: "#e5544b",
};

// Visualizes an asset's generation status/provenance and its version history so
// placeholders → replacements are visible and a prior version can be reverted to.
const AssetPanel: React.FC<{
  asset: CompositionAsset | null;
  onRevert: (assetId: string, version: number) => void;
  onApprove: (assetId: string, approved: boolean) => void;
}> = ({ asset, onRevert, onApprove }) => {
  if (!asset) {
    return (
      <div style={{ fontSize: 11, color: "#5f5f68", marginBottom: 10 }}>
        Designed layer — no external asset.
      </div>
    );
  }
  const prev = (asset as unknown as { previousVersions?: Array<{ version: number }> }).previousVersions ?? [];
  return (
    <div style={{ marginBottom: 12, padding: 8, border: "1px solid #232329", borderRadius: 6 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
        <span style={{ width: 8, height: 8, borderRadius: 8, background: STATUS_COLOR[asset.status] || "#8a93a3" }} />
        <span style={{ fontSize: 11, color: "#ececef" }} data-testid="asset-status">
          {asset.status.toUpperCase()} · v{asset.version}
        </span>
        <span style={{ marginLeft: "auto", fontSize: 10, color: asset.approved ? "#4fc283" : "#5f5f68" }}>
          {asset.approved ? "approved" : "unapproved"}
        </span>
      </div>
      <div style={{ fontSize: 10, color: "#5f5f68", wordBreak: "break-all" }}>
        {asset.url ? asset.url : asset.status === "placeholder" ? "placeholder (no media yet)" : "—"}
      </div>
      {asset.provenance ? (
        <div style={{ fontSize: 10, color: "#8a93a3", marginTop: 3 }}>
          {[asset.provenance.provider, asset.provenance.model, asset.provenance.tool]
            .filter(Boolean)
            .join(" · ") || "no provenance"}
        </div>
      ) : null}
      <div style={{ display: "flex", gap: 6, marginTop: 6, flexWrap: "wrap" }}>
        <button style={miniBtn} onClick={() => onApprove(asset.id, !asset.approved)}>
          {asset.approved ? "Unapprove" : "Approve"}
        </button>
        {prev.map((p) => (
          <button key={p.version} style={miniBtn} onClick={() => onRevert(asset.id, p.version)}>
            Revert v{p.version}
          </button>
        ))}
      </div>
    </div>
  );
};

const ValidationPanel: React.FC<{ result: ReturnType<typeof validateComposition> }> = ({ result }) => (
  <div>
    <div style={{ fontSize: 12, color: "#a0a0a9", marginBottom: 6 }}>
      VALIDATION · {result.ok ? "structurally valid" : "errors"} ·{" "}
      {result.renderReady ? "render-ready" : "not render-ready"}
    </div>
    {result.errors.slice(0, 4).map((e, i) => (
      <div key={i} style={{ fontSize: 11, color: "#e5544b" }}>
        ✕ {e.message}
      </div>
    ))}
    {result.warnings.slice(0, 4).map((w, i) => (
      <div key={i} style={{ fontSize: 11, color: "#e8c07d" }}>
        ⚠ {w.message}
      </div>
    ))}
  </div>
);

// Shown in place of a blank canvas when the timeline has no layers yet. Its SINGLE
// primary action is "Add first scene", wired to the SAME real add-layer flow as
// the toolbar "+ Add layer" — a click actually creates a first layer/scene.
const EmptyTimelineCard: React.FC<{
  target: import("../composition/status").OverviewTarget | null;
  usedFixture: boolean;
  onAddFirstScene: () => void;
}> = ({ target, usedFixture, onAddFirstScene }) => {
  return (
    <div style={{ maxWidth: 560, textAlign: "center", padding: 28 }} data-testid="empty-timeline">
      <div style={{ fontSize: 42, marginBottom: 12, opacity: 0.5 }}>🎬</div>
      <div style={{ fontSize: 18, fontWeight: 650, color: "#ececef", marginBottom: 8 }}>
        Your timeline is empty
      </div>
      <div style={{ fontSize: 13.5, color: "#a0a0a9", lineHeight: 1.5, marginBottom: 16 }}>
        This is a manual editor. Add your first scene to start building — then move,
        trim, split, and layer scenes on the timeline, and render when you are ready.
      </div>
      <button style={btnAccent} data-testid="add-first-scene" onClick={onAddFirstScene}>
        + Add first scene
      </button>
      {/* Truthful target duration — never the invented composer default. */}
      <div style={{ fontSize: 12, color: "#8a93a3", fontFamily: "ui-monospace, monospace", margin: "14px 0 6px" }} data-testid="empty-target">
        {target?.available ? target.label : "Duration set after first scene"}
      </div>
      <div style={{ fontSize: 12, color: "#5f5f68" }}>
        {usedFixture
          ? "Demo mode — sample data, no project on disk."
          : "Nothing is rendering — the render button unlocks once there are scenes to render."}
      </div>
    </div>
  );
};

const Field: React.FC<{ label: string; children: React.ReactNode }> = ({ label, children }) => (
  <label style={{ display: "block", marginBottom: 8 }}>
    <span style={{ fontSize: 11, color: "#5f5f68", display: "block", marginBottom: 3 }}>{label}</span>
    {children}
  </label>
);

const Centered: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <div style={{ height: "100%", display: "flex", alignItems: "center", justifyContent: "center", color: "#a0a0a9", padding: 20, textAlign: "center" }}>
    {children}
  </div>
);

// Frames the title's spring entrance needs before it reaches a legible opacity —
// so a post-create seek lands where the new scene is actually visible (the render
// itself is unchanged and still starts at frame 0).
const ENTRANCE_VISIBLE_FRAMES = 24;

function addNewLayer(
  model: CanonicalComposition,
  apply: (fn: (c: CanonicalComposition) => CanonicalComposition) => void,
  setSelected: (id: string) => void,
): number {
  const type: LayerType = "text";
  const id = makeId(`layer`, model.layers.length + 1 + model.totalFrames);
  const isFirst = model.layers.length === 0;
  // The FIRST scene spans the whole timeline so it is visible in the preview at
  // any playhead (and reads as a legible block, not a 2-char sliver). Subsequent
  // scenes get a sensible default chunk the user can move/resize.
  const total = Math.max(1, model.totalFrames);
  const durationFrames = isFirst ? total : Math.min(150, total);
  const layer: Layer = {
    id,
    type,
    trackId: trackKindForType(type),
    startFrame: 0,
    durationFrames,
    z: (model.layers.reduce((m, l) => Math.max(m, l.z), 0) || 0) + 1,
    enabled: true,
    locked: false,
    opacity: 1,
    // A visible, centered, high-contrast default the user edits in the Inspector.
    text: "New scene",
  };
  apply((c) => addLayer(c, layer));
  setSelected(id);
  // A representative visible frame INSIDE the new layer (bounded by its real
  // effective duration) — where the title's entrance has faded fully in.
  return Math.max(1, Math.min(ENTRANCE_VISIBLE_FRAMES, durationFrames - 1));
}

// ── styles ──
const workspaceStrip: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  gap: 12,
  padding: "8px 14px",
  borderBottom: "1px solid #232329",
  background: "#0d0d10",
};
const header: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  padding: "10px 14px",
  borderBottom: "1px solid #232329",
  background: "#101013",
  flexWrap: "wrap",
  gap: 8,
};
const rail: React.CSSProperties = {
  width: 240,
  borderRight: "1px solid #232329",
  background: "#0c0c0f",
  padding: 12,
  overflowY: "auto",
};
const railTitle: React.CSSProperties = { fontSize: 11, color: "#5f5f68", letterSpacing: "0.08em", marginBottom: 10 };
const domainTag: React.CSSProperties = {
  fontFamily: "ui-monospace, monospace",
  fontSize: 9,
  letterSpacing: "0.18em",
  textTransform: "uppercase",
  color: "#6aa1ff",
  marginRight: 4,
};
const domainStrip: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  padding: "6px 12px",
  borderBottom: "1px solid #232329",
  background: "#0c0c0f",
};
const trackHeader: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  fontSize: 11,
  color: "#a0a0a9",
  marginBottom: 4,
  textTransform: "uppercase",
  letterSpacing: "0.06em",
};
const inspector: React.CSSProperties = {
  width: 340,
  flex: "0 0 auto",
  borderLeft: "1px solid #232329",
  background: "#0c0c0f",
  padding: 12,
  overflowY: "auto",
};
const stage: React.CSSProperties = {
  flex: 1,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  background: "radial-gradient(circle at 50% 40%, #131318, #0a0a0c)",
  padding: 16,
  overflow: "auto",
  minHeight: 0,
};
const transport: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  padding: "8px 12px",
  borderTop: "1px solid #232329",
  background: "#101013",
};
const pill: React.CSSProperties = {
  fontSize: 11,
  color: "#a0a0a9",
  border: "1px solid #232329",
  borderRadius: 999,
  padding: "2px 10px",
};
const btn: React.CSSProperties = {
  background: "#16161a",
  color: "#ececef",
  border: "1px solid #232329",
  borderRadius: 6,
  padding: "5px 10px",
  fontSize: 12,
  cursor: "pointer",
};
const btnPrimary: React.CSSProperties = { ...btn, background: "#1c1c21", fontWeight: 600 };
const btnAccent: React.CSSProperties = { ...btn, background: "#e8c07d", color: "#1a1a1a", border: "1px solid #e8c07d", fontWeight: 700 };
const miniBtn: React.CSSProperties = { ...btn, padding: "3px 8px", fontSize: 11 };
const inp: React.CSSProperties = {
  width: "100%",
  background: "#16161a",
  color: "#ececef",
  border: "1px solid #232329",
  borderRadius: 6,
  padding: "6px 8px",
  fontSize: 12,
  boxSizing: "border-box",
};
const noticeBar: React.CSSProperties = {
  padding: "6px 14px",
  background: "#16161a",
  borderBottom: "1px solid #232329",
  fontSize: 12,
  color: "#e8c07d",
};
