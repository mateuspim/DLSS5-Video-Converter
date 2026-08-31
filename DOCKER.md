# Docker on Linux and WSL

## What works

The container runs the web UI, video and static-image decoding, portable CPU or
OpenCL GPU preview filter, FFmpeg encoding, audio/metadata muxing, result
verification, and reports on Linux, Docker Desktop, and WSL 2. PNG, JPEG, and
WebP inputs produce lossless PNG outputs.

The portable backends are **not NVIDIA DLSS**. Their files contain `_SOFTWARE_`
or `_GPU_`, and their reports set `feature_18_confirmed` to `false`. The
upstream feature-18 worker is a Windows D3D12 executable that loads a Windows
DLL; Linux containers cannot execute it through the NVIDIA Container Toolkit.

## Use the NVIDIA GPU on Linux

Install and configure the NVIDIA Container Toolkit for Docker, then start the
OpenCL GPU override:

```bash
docker compose -f compose.yaml -f compose.gpu.yaml up --build
```

This selects `DLSS5_BACKEND=gpu`, requests the NVIDIA GPU from Docker, and
forces OpenCV's Transparent API to select an NVIDIA OpenCL GPU. It refuses to
start if OpenCL would fall back to the CPU. Results contain `_GPU_`, use the
`portable-opencl-gpu-preview` pipeline label, and still set
`feature_18_confirmed` to `false` because this is GPU acceleration for the
portable filter—not the Windows DLSS model.

## Build the Windows worker from Linux or WSL

The worker is C++, but NVIDIA's supplied library contains MSVC C++ ABI objects;
an ordinary Linux cross-compiler cannot link it without Microsoft's proprietary
toolchain libraries. The included wrapper dispatches an MSVC build to the
fork's Windows GitHub runner and downloads the resulting artifact:

```bash
./scripts/build-windows-worker.sh
```

The result is `bin/runtime/nvngx.dll`. Despite its extension, it is a Windows
console executable whose filename intentionally satisfies the model's caller
check. It cannot execute inside the Linux container; copy it and the patched
`nvngx_dlssnr.dll` to the Windows-side `bin/runtime` directory.

The wrapper requires an authenticated [GitHub CLI](https://cli.github.com/).
Override `DLSS5_GITHUB_REPOSITORY` and `DLSS5_GITHUB_BRANCH` when building from
another fork or branch.

## Start

```bash
docker compose up --build
```

Open <http://127.0.0.1:7860>. Results persist under `data/outputs` and copies of
successful inputs under `data/originals`.

Stop the service with:

```bash
docker compose down
```

## WSL 2

Run the same commands from a WSL 2 distribution with Docker Desktop WSL
integration enabled, or with Docker Engine installed inside WSL. Keep the
repository in the Linux filesystem for substantially better bind-mount
performance than `/mnt/c`.

## Configuration

- `DLSS5_PORT`: host port, default `7860`.
- `DLSS5_MAX_UPLOAD_BYTES`: upload limit, default 20 GiB.
- `DLSS5_MAX_IMAGE_BYTES`: compressed image limit, default 256 MiB.
- `DLSS5_MAX_IMAGE_PIXELS`: decoded image limit, default 100 megapixels.
- `DLSS5_BACKEND`: `software`, `gpu`, `relay`, `dlss`, or `auto`. The Docker image
  defaults to `software`; selecting local `dlss` in Linux fails explicitly.

## Actual DLSS feature 18

Actual feature-18 rendering remains Windows-native, but WSL can keep the UI and
FFmpeg pipeline in Docker and stream frames to the included authenticated relay.

On Windows, place the upstream runtime under `bin/runtime`, generate a strong
random token, and run from PowerShell:

```powershell
$env:DLSS5_RELAY_TOKEN = "replace-with-a-long-random-secret"
bin\python-3.13.15-embed-amd64\python.exe relay\windows_relay.py --host 0.0.0.0
```

Allow TCP port 17861 only on the private WSL/Docker interface. The relay has no
TLS, so do not expose it to a LAN or the internet.

From WSL, use the same token and start the relay override:

```bash
export DLSS5_RELAY_TOKEN='replace-with-the-same-long-random-secret'
docker compose -f compose.yaml -f compose.relay.yaml up --build
```

Docker Desktop normally resolves `host.docker.internal` to Windows. If using a
different Docker/WSL arrangement, set `DLSS5_RELAY_URL` to the Windows host IP
and port. The relay accepts one GPU job at a time and forwards only the worker's
binary frame protocol; it does not accept paths or execute caller-supplied
commands.
