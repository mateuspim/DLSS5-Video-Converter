# DLSS 5 Video Converter

Local web tool that runs video through **NVIDIA DLSS 5 Neural Rendering** (NGX feature 18, `nvngx_dlssnr.dll`) with optical-flow motion vectors and encodes the result with NVENC.

> **Experimental.** This is a research build that calls the DLSS Neural Rendering model directly, outside of a game engine. Read the [Limitations](#limitations) section before using it.

---

## What it does

1. You drop a video file into the web UI (or pick it with the file dialog).
2. Every frame is decoded, converted to RGBA8, and sent to the DLSS 5 Neural Rendering model (feature 18) together with optical-flow motion vectors estimated from the video itself.
3. The processed frames are encoded with NVENC (H.264 / HEVC / AV1) and muxed back with the original audio and metadata.
4. The result lands in `outputs/` next to a JSON report with full pipeline evidence (feature-18 confirmation, per-frame evaluation count, model SHA-256, encoder logs).

## Quick start

**Requirements:** Windows 10/11 64-bit, NVIDIA RTX GPU, recent driver. Everything else (Python, FFmpeg, the DLSS runtime) is bundled — no installation needed.

1. Run `start.bat`.
2. Open **http://127.0.0.1:7860** in your browser.
3. Drag a video onto the drop zone (or click it), pick a profile and encoding settings, press **Render whole video**.

The server binds to `127.0.0.1` only — the UI is not exposed to the network.

## Features

### Profiles

Preset combinations of Neural Rendering parameters:

| Profile | Intensity | Local Tone | Local Structure | Skin Structure |
| --- | --- | --- | --- | --- |
| Faithful | 0.70 | 0.75 | 0.75 | −1.0 |
| Natural | 1.00 | 1.00 | 1.00 | −1.0 |
| Strong / Cinematic | 1.65 | 1.40 | 1.50 | 1.0 |
| Extreme / Overdrive | 2.50 | 2.00 | 2.00 | 1.5 |

### NR Parameters (manual)

Four sliders — **Intensity**, **Local Tone**, **Local Structure** (range 0–3) and **Skin Structure** (range −1 to 3), step 0.05. Moving any slider overrides the selected profile for that parameter.

### Encoding

- **Codecs:** H.264, HEVC, AV1 — NVENC when available; H.264/HEVC fall back to `libx264`/`libx265` if NVENC is missing. AV1 requires NVENC support (no software fallback).
- **Containers:** MP4 (audio re-encoded to AAC 192 kbps, `+faststart`) or MKV (audio/subtitles copied).
- **Quality:** High (CRF 17), Balanced (CRF 20), Small (CRF 24).

### Interface

- RU/EN language toggle, dark/light theme (both remembered between sessions).
- Drag & drop or "Add video" button; supported input: mp4 / mkv / webm.
- Live progress bar, per-frame status, GPU badge (name + driver version).
- Results feed with **Download** and **JSON report** buttons, plus an in-page preview of the last render.
- **Stop** cancels the current render (partial video is removed, diagnostics are kept); **Clear** empties the results feed.
- One render at a time; the UI refuses to start a second job while one is running.

## How it works (pipeline)

```
input video → ffprobe analysis → per-frame decode (PyAV)
    → RGBA8 frames + optical-flow motion vectors (DIS, 640 px)
    → native worker: direct feature-18 evaluation (nvngx_dlssnr.dll)
    → rawvideo stream → NVENC (H.264/HEVC/AV1)
    → mux with original audio/metadata → outputs/<name>_DLSS5_<stamp>.mp4|.mkv
    → frame-count verification + JSON report
```

- Motion vectors are estimated with OpenCV DIS optical flow (current-to-previous, pixel units) and a scene-cut detector resets the temporal state when the frame difference exceeds a threshold.
- The first 120 frames are used as a warm-up for the model.
- After muxing, the output is re-probed and the frame count must match the render exactly — otherwise the job fails rather than shipping a truncated file.
- Every frame must return a successful feature-18 result; any failed evaluation aborts the render.

## Requirements

| Component | Requirement |
| --- | --- |
| OS | Windows 10/11, 64-bit |
| GPU | NVIDIA RTX 40/50 series (RTX 30 series: **beta** — works but very slow) |
| Driver | Recent NVIDIA driver (NVENC + NGX support) |
| RAM | Enough to hold the video frames being processed (4K needs several GB) |
| Network | None — fully local |

The tool checks the GPU at startup via `nvidia-smi` and refuses to run on non-RTX hardware. FFmpeg, an embedded Python, and the DLSS runtime are shipped in `bin/` — no system installs required.

## Limitations (read this)

- **SDR only.** The verified DLSSNR path renders in RGBA8. HDR input is converted to SDR and the output is **not** labeled as HDR — HDR preservation is disabled in this build on purpose, rather than mislabeling the result.
- **Estimated motion vectors.** The optical flow used here is computed from the video itself and is *not* the engine motion vectors the model was designed for. Expect temporal artifacts on fast motion, occlusions, thin objects, and scene cuts.
- **Modified binaries.** `bin/runtime/nvngx.dll` is a modified shim (a wrapper/worker image), not the NVIDIA NGX core. `nvngx_dlssnr.dll` is a modified model build patched for standalone (out-of-engine) calls — it includes a caller check. See `others/THIRD_PARTY.md` for hashes and licensing.
- **Strict failure policy.** If any frame fails its feature-18 evaluation, the whole render is rejected. This is intentional — no unverifiable output.
- **Slow first analysis.** On 4K video the initial probe can take 1–3 minutes, because ffprobe decodes every frame to get an exact count.
- **Speed.** Rendering is per-frame and CPU-side flow estimation is part of the pipeline; expect long runtimes on long or high-resolution videos. RTX 30 series is beta and very slow.
- **One job at a time.** No queue.

## Project layout

```
merserk-0.1/
├── start.bat                  # launcher (embedded Python → app.py)
├── app.py                     # web UI (Python http.server, port 7860)
├── dlss5_converter/
│   ├── core.py                # backend: probe, feature-18 pipeline, NVENC, reports
│   └── guides.py              # optical-flow + scene-cut guide generation
├── bin/
│   ├── ffmpeg/                # bundled FFmpeg/ffprobe
│   ├── python-3.13.15-embed-amd64/   # embedded Python
│   └── runtime/               # nvngx.dll (worker) + nvngx_dlssnr.dll (model)
├── outputs/                   # rendered videos + .report.json
├── jobs/                      # per-job temp files (cleaned up)
├── _work/                     # upload staging
├── native/                    # DLSS5-Feeder host source, NVIDIA DLSS SDK headers
└── others/THIRD_PARTY.md      # third-party components and hashes
```

## JSON report

Every successful render writes `<output>.report.json` next to the video. It contains the input/output metadata, chosen options, GPU info, the encoder actually used, frame count, scene resets, elapsed time, average FPS, the feature-18 confirmation (`feature_18_confirmed`, `direct_create_result`, `successful_direct_evaluations`), SHA-256 of the model and worker DLLs, and the raw worker/encoder logs.

## Author

**Perseval** — https://youtube.com/@perseval_BLR

## License / third-party

This is an experimental local build. DLSS5-Feeder (MIT) and the NVIDIA DLSS SDK licenses are in `native/`; FFmpeg licensing is in `bin/ffmpeg/LICENSE`. See `others/THIRD_PARTY.md` for the staged runtime hashes and redistribution notes.
