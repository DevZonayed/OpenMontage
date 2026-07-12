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
  setVolume,
  splitLayer,
  trimLayer,
} from "../composition/operations";
import { BacklotClient } from "../composition/client";

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
  const [run, setRun] = useState<Record<string, unknown>>({ state: "not_started" });
  const [rendering, setRendering] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const playerRef = useRef<PlayerRef>(null);

  const model = histRef.current?.present ?? null;

  // ── load ──
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
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [client, rerender]);

  const refreshStatus = useCallback(async () => {
    try {
      const r = await client.getRun();
      setRun(r);
    } catch {
      /* ignore */
    }
  }, [client]);

  useEffect(() => {
    void load();
    void refreshStatus();
  }, [load, refreshStatus]);

  // Live status via SSE (falls back silently in fixture mode).
  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    let es: EventSource | null = null;
    try {
      es = new EventSource(`/api/project/${client.projectId}/events`);
      es.onmessage = () => void refreshStatus();
    } catch {
      /* offline / fixtures */
    }
    const poll = setInterval(() => void refreshStatus(), 4000);
    return () => {
      es?.close();
      clearInterval(poll);
    };
  }, [client.projectId, refreshStatus]);

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
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setNotice(msg.includes("changed") ? `Conflict: ${msg}. Reload to merge.` : `Save failed: ${msg}`);
    } finally {
      setSaving(false);
    }
  }, [client, etag]);

  const renderFinal = useCallback(async () => {
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

  // ── transport ──
  const seek = useCallback((f: number) => {
    const p = playerRef.current;
    if (!p || !model) return;
    const clamped = Math.max(0, Math.min(model.totalFrames - 1, Math.round(f)));
    p.seekTo(clamped);
    setFrame(clamped);
  }, [model]);

  const togglePlay = useCallback(() => playerRef.current?.toggle(), []);
  const step = useCallback((delta: number) => seek(frame + delta), [frame, seek]);

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
          if (model) seek(model.totalFrames - 1);
          break;
        default:
          break;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [redo, save, seek, step, togglePlay, undo, model]);

  const inputProps: TimelineFrameProps | null = useMemo(
    () => (model ? (renderProps(model) as TimelineFrameProps) : null),
    [model],
  );

  if (loading) {
    return <Centered>Loading production studio…</Centered>;
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

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", color: "#ececef" }}>
      {/* Header / actions */}
      <div style={header}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <strong style={{ fontSize: 15 }}>{model.meta.title || client.projectId}</strong>
          <span style={pill}>
            {model.width}×{model.height} · {model.fps}fps · {model.totalFrames} frames ·{" "}
            {model.meta.targetFormatted || `${model.targetDurationSeconds}s`}
          </span>
          {usedFixture ? <span style={{ ...pill, borderColor: "#e8c07d", color: "#e8c07d" }}>fixture / offline</span> : null}
          <span
            style={{ ...pill, color: renderReady ? "#4fc283" : "#e8c07d", borderColor: renderReady ? "#4fc283" : "#e8c07d" }}
            title={renderReason}
          >
            {renderReady ? "Remotion ready" : "Remotion not ready"}
          </span>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <button style={btn} onClick={undo} disabled={!histRef.current?.canUndo} aria-label="Undo">
            Undo
          </button>
          <button style={btn} onClick={redo} disabled={!histRef.current?.canRedo} aria-label="Redo">
            Redo
          </button>
          <button style={{ ...btnPrimary, opacity: dirty ? 1 : 0.6 }} onClick={() => void save()} disabled={saving}>
            {saving ? "Saving…" : dirty ? "Save*" : "Saved"}
          </button>
          <button style={btnAccent} onClick={() => void renderFinal()} disabled={rendering}>
            {rendering ? "Rendering…" : "▶ Render final film"}
          </button>
        </div>
      </div>

      {notice ? <div style={noticeBar}>{notice}</div> : null}

      <div style={{ display: "flex", flex: 1, minHeight: 0 }}>
        {/* Scene rail / tracks / layers */}
        <div style={rail}>
          <div style={railTitle}>SCENES · TRACKS · SEQUENCES</div>
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
          <button style={{ ...miniBtn, marginTop: 8 }} onClick={() => addNewLayer(model, apply, setSelected)}>
            + Add layer
          </button>
        </div>

        {/* Player + transport */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
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

          <Transport
            frame={frame}
            totalFrames={model.totalFrames}
            fps={model.fps}
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

        {/* Inspector + live status */}
        <div style={inspector}>
          <LiveStatus run={run} />
          <div style={{ height: 1, background: "#232329", margin: "12px 0" }} />
          {selectedLayer ? (
            <Inspector
              key={selectedLayer.id}
              layer={selectedLayer}
              onEdit={(edit) => apply((c) => trimLayer(c, selectedLayer.id, edit))}
              onMove={(f) => apply((c) => moveLayer(c, selectedLayer.id, f))}
              onResize={(d) => apply((c) => resizeLayer(c, selectedLayer.id, d))}
              onVolume={(v) => apply((c) => setVolume(c, selectedLayer.id, v))}
              onSplit={() => apply((c) => splitLayer(c, selectedLayer.id, frame).composition)}
              onDelete={() => {
                apply((c) => removeLayer(c, selectedLayer.id));
                setSelected(null);
              }}
              onRegen={async (instr) => {
                try {
                  await client.queueRevision(selectedLayer.id, instr);
                  setNotice(`Queued regeneration for ${selectedLayer.id}. Its stable id + approval are preserved.`);
                } catch (e) {
                  setNotice(`Could not queue: ${e instanceof Error ? e.message : String(e)}`);
                }
              }}
            />
          ) : (
            <div style={{ color: "#5f5f68", fontSize: 13 }}>Select a layer to edit it.</div>
          )}
          <div style={{ height: 1, background: "#232329", margin: "12px 0" }} />
          <ValidationPanel result={validation} />
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
  frame: number;
  totalFrames: number;
  fps: number;
  playing: boolean;
  previewZoom: number;
  onZoom: (z: number) => void;
  onToggle: () => void;
  onSeek: (f: number) => void;
  onStep: (d: number) => void;
}> = ({ frame, totalFrames, fps, playing, previewZoom, onZoom, onToggle, onSeek, onStep }) => (
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
    <span style={{ fontFamily: "ui-monospace, monospace", fontSize: 12, minWidth: 150, textAlign: "right" }}>
      {timecode(frame, fps)} · f{frame}/{totalFrames}
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
                    width: Math.max(3, l.durationFrames * pxPerFrame),
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
  onEdit: (edit: { startFrame?: number; durationFrames?: number }) => void;
  onMove: (f: number) => void;
  onResize: (d: number) => void;
  onVolume: (v: number) => void;
  onSplit: () => void;
  onDelete: () => void;
  onRegen: (instr: string) => void;
}> = ({ layer, onEdit, onMove, onResize, onVolume, onSplit, onDelete, onRegen }) => {
  const [instr, setInstr] = useState("");
  return (
    <div>
      <div style={{ fontSize: 12, color: "#a0a0a9", marginBottom: 8 }}>
        LAYER · {layer.type} · {layer.id}
      </div>
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
      <div style={{ marginTop: 14 }}>
        <div style={{ fontSize: 12, color: "#a0a0a9", marginBottom: 6 }}>SELECTIVE REGENERATION</div>
        <textarea
          value={instr}
          onChange={(e) => setInstr(e.target.value)}
          placeholder="How should this asset change? (id + approval preserved)"
          style={{ ...inp, height: 52, resize: "vertical" }}
        />
        <button
          style={{ ...miniBtn, marginTop: 6 }}
          onClick={() => {
            if (instr.trim()) onRegen(instr.trim());
          }}
        >
          Queue regeneration
        </button>
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

const LiveStatus: React.FC<{ run: Record<string, unknown> }> = ({ run }) => {
  const state = String(run.state ?? "not_started");
  const phase = String((run.phase as string) ?? (run.active_stage as string) ?? "—");
  const label =
    state === "not_started"
      ? "NOT STARTED"
      : state === "running"
        ? "PRODUCTION RUNNING"
        : state === "waiting_for_approval"
          ? "AWAITING APPROVAL"
          : state.toUpperCase();
  const color =
    state === "running" ? "#4fc283" : state === "waiting_for_approval" ? "#e8c07d" : "#5f5f68";
  const log = Array.isArray(run.log) ? (run.log as unknown[]).slice(-4) : [];
  return (
    <div>
      <div style={{ fontSize: 12, color: "#a0a0a9", marginBottom: 6 }}>LIVE PRODUCTION</div>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span style={{ width: 8, height: 8, borderRadius: 8, background: color }} />
        <strong style={{ fontSize: 13, color }}>{label}</strong>
      </div>
      <div style={{ fontSize: 11, color: "#a0a0a9", marginTop: 4 }}>phase: {phase}</div>
      {log.map((l, i) => (
        <div key={i} style={{ fontSize: 10, color: "#5f5f68", marginTop: 2 }}>
          {typeof l === "string" ? l : JSON.stringify(l)}
        </div>
      ))}
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

function addNewLayer(
  model: CanonicalComposition,
  apply: (fn: (c: CanonicalComposition) => CanonicalComposition) => void,
  setSelected: (id: string) => void,
) {
  const type: LayerType = "text";
  const id = makeId(`layer`, model.layers.length + 1 + model.totalFrames);
  const layer: Layer = {
    id,
    type,
    trackId: trackKindForType(type),
    startFrame: 0,
    durationFrames: Math.min(90, model.totalFrames),
    z: (model.layers.reduce((m, l) => Math.max(m, l.z), 0) || 0) + 1,
    enabled: true,
    locked: false,
    opacity: 1,
    text: "New layer",
  };
  apply((c) => addLayer(c, layer));
  setSelected(id);
}

// ── styles ──
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
  width: 280,
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
