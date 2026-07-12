# QA — deterministic render evidence (Worker B, PR #3)

All render artifacts live under `qa/out/` (gitignored). Input media lives under
`public/qa-*` (gitignored via `remotion-composer/public/*`). Nothing here is committed.
No paid generation — inputs are synthesized locally with FFmpeg.

## Reproduce

```bash
cd remotion-composer && npm ci

# 1. Deterministic inputs (free, local):
ffmpeg -y -f lavfi -i "sine=frequency=440:duration=8" -f lavfi -i "sine=frequency=554:duration=8" \
  -filter_complex "[0][1]amix=inputs=2:normalize=0,volume=0.5[a]" -map "[a]" -c:a libmp3lame -q:a 4 public/qa-tone.mp3
ffmpeg -y -f lavfi -i "testsrc2=size=1920x1080:duration=1" -frames:v 1 public/qa-photo.png

# 2. Serve inputs so the headless render browser can fetch them (absolute URLs):
python3 -m http.server 4760 --bind 127.0.0.1 --directory public &

# 3. Render with the SAFE PINNED wrapper ONLY (no npx, no PATH remotion):
node scripts/remotion-cli.mjs render src/index.tsx TimelineFrame qa/out/qa_render.mp4 \
  --props=qa/props_render.json --log=error

# 4. Prove the 5-minute metadata invariant without a full encode:
node scripts/remotion-cli.mjs compositions src/index.tsx --props=qa/props_9000.json
#   → TimelineFrame  30  1920x1080  9000 (300.00 sec)
```

## Results (observed)

Artifact: `remotion-composer/qa/out/qa_render.mp4`
SHA-256: `10e7ae05e4200144a5b7780eb8316ac108f0549d1f04fc21ec5af5e36156e09c`
Size: 6,560,288 bytes

ffprobe:
- container: mov/mp4, duration 8.042667 s
- video: **H.264 (High), 1920x1080, 30/1 fps**
- audio: **AAC LC, 48000 Hz, stereo (2 ch)**
- decoded video frame count (`-count_frames`): **240** (= 8 s × 30 fps, exact)

Probes:
- `blackdetect` (d=0.05, pic_th=0.98): **no black frames**
- `silencedetect` (-50 dB, d=0.3): **no silence** (continuous tone)
- `volumedetect`: mean **-29.5 dB**, max **-22.8 dB**
- `ebur128` integrated loudness: **-26.9 LUFS**

Metadata invariant: `TimelineFrame` reports **9000 (300.00 sec)** with the 300 s
props — the 5-minute = 9000-frame contract, proven from composition metadata
without an expensive full encode.

Visual inspection (`qa/out/insp_title.png`, `qa/out/insp_caption.png`): real image
(FFmpeg testsrc2 color bars) composited via `<Img>`, kinetic title "PREVIEW = RENDER"
with subtitle, "Real media" lower-third, and the "♪ MUSIC · 80%" audio presence strip.
