# Docker on Linux and WSL

## What works

The container runs the web UI, video decoding, portable CPU preview filter,
FFmpeg encoding, audio/metadata muxing, result verification, and reports on
Linux, Docker Desktop, and WSL 2.

The portable backend is **not NVIDIA DLSS**. Its files contain `_SOFTWARE_`,
and its reports set `feature_18_confirmed` to `false`. The upstream feature-18
worker is a Windows D3D12 executable that loads a Windows DLL; Linux containers
cannot execute it through the NVIDIA Container Toolkit.

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
- `DLSS5_BACKEND`: `software`, `relay`, `dlss`, or `auto`. The Docker image
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
