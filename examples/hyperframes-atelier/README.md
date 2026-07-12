# Example — HyperFrames atelier composition ("Code Into Film")

A hand-authored, bespoke **HyperFrames** composition rendered deterministically to
video with **zero API keys**. This is the reusable *source* (tracked); the rendered
MP4 and per-project checkpoints are regenerable and stay under the gitignored
`projects/` workspace.

- `index.html` — the composition (HTML/CSS/GSAP to the HyperFrames `data-*` contract).
- `art-direction.md` — the design read + scene plan (distinct primary subject per scene).

It demonstrates the atelier path the tools grew (`hyperframes_compose` `prebuilt=True`
+ `video_compose` `composition_mode="atelier"`, closing the documented gap in
`skills/meta/bespoke-composition.md`), plus a persistent branded HUD so no transition
frame is ever bare.

## Reproduce

Requirements: the HyperFrames runtime floor — Node ≥ 22, `ffmpeg`, `npx`
(`make hyperframes-doctor` verifies it). No API keys.

Verify the source, then render through the governed router:

```bash
# 1. Lint + validate + spot-check the tracked composition
cd examples/hyperframes-atelier
npx hyperframes lint . && npx hyperframes validate .
npx hyperframes snapshot . --at 1.8,5.4,9,13.2,16       # optional contact sheet

# 2. Render via the OpenMontage governed atelier path (from repo root)
python - <<'PY'
from tools.video.video_compose import VideoCompose
r = VideoCompose().execute({
    "operation": "render",
    "edit_decisions": {"version": "1.0", "render_runtime": "hyperframes",
                       "composition_mode": "atelier", "renderer_family": "animation-first", "cuts": []},
    "output_path": "projects/openmontage-atelier/renders/final.mp4",
    "workspace_path": "examples/hyperframes-atelier",
})
print("ok:", r.success, "| review:", (r.data or {}).get("final_review", {}).get("status"))
PY
```

Output: an 18s 1920×1080 h264 MP4. Silent by design (local zero-key TTS/BGM are not
assumed installed; no paid API is used).

## Test

`tests/tools/test_hyperframes_atelier.py::TestTrackedExample` validates this source's
HyperFrames contract deterministically, and (gated by
`OPENMONTAGE_RUN_HYPERFRAMES_RENDER=1`) renders it end-to-end.
