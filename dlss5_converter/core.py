from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable

import av
import cv2
import numpy as np

from .guides import TemporalGuideGenerator

# PyInstaller exe: ассеты (bin/, outputs/, jobs/, originals/) лежат рядом с exe,
# а __file__ указывает на _MEIPASS (временная распаковка) — берём папку exe.
if getattr(sys, "frozen", False):
    BASE = Path(sys.executable).resolve().parent
else:
    BASE = Path(__file__).resolve().parents[1]

ROOT = BASE
RUNTIME = ROOT / "bin" / "runtime"

VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def _media_tool(env_name: str, bundled_name: str, system_name: str) -> Path:
    override = os.environ.get(env_name)
    if override:
        return Path(override)
    bundled = ROOT / "bin" / "ffmpeg" / "bin" / bundled_name
    if bundled.exists():
        return bundled
    discovered = shutil.which(system_name)
    return Path(discovered) if discovered else bundled


FFMPEG = _media_tool("DLSS5_FFMPEG", "ffmpeg.exe", "ffmpeg")
FFPROBE = _media_tool("DLSS5_FFPROBE", "ffprobe.exe", "ffprobe")
WORKER = RUNTIME / "nvngx.dll"  # executable image name required by the signed snippet caller check
OUTPUTS = ROOT / "outputs"
JOBS = ROOT / "jobs"
ORIGINALS = ROOT / "originals"

VIDEO_MAGIC = 0x32563544
FRAME_MAGIC = 0x314D5246
OUT_MAGIC = 0x3154554F


@dataclass(slots=True)
class ConversionOptions:
    profile: str = "Strong / Cinematic"
    codec: str = "H.264"
    container: str = "MP4"
    quality: str = "High"
    preserve_hdr: bool = False
    warmup_frames: int = 120
    # Кастомные NR-параметры (переопределяют профиль, если заданы)
    intensity: float | None = None
    local_tone: float | None = None
    local_structure: float | None = None
    skin_structure: float | None = None


PROFILES = {
    "Faithful": dict(profile=0, preset=0, style=0, auto_mask=0, ui_correction=0,
                     intensity=0.70, local_tone=0.75, local_structure=0.75, skin_structure=-1.0),
    "Natural": dict(profile=1, preset=0, style=1, auto_mask=0, ui_correction=0,
                    intensity=1.00, local_tone=1.00, local_structure=1.00, skin_structure=-1.0),
    "Strong / Cinematic": dict(profile=2, preset=2, style=2, auto_mask=1, ui_correction=0,
                               intensity=1.65, local_tone=1.40, local_structure=1.50, skin_structure=1.0),
    "Extreme / Overdrive": dict(profile=2, preset=2, style=2, auto_mask=1, ui_correction=0,
                                intensity=2.50, local_tone=2.00, local_structure=2.00, skin_structure=1.5),
}


@dataclass(slots=True)
class ConversionResult:
    output_path: str
    report_path: str
    frames: int
    nr_count_evidence: int
    elapsed_seconds: float
    gpu: str
    backend: str
    media_type: str = "video"


class Cancelled(RuntimeError):
    pass


class JobController:
    def __init__(self) -> None:
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.processes: list[subprocess.Popen] = []

    def register(self, process: subprocess.Popen) -> None:
        with self.lock:
            self.processes.append(process)

    def unregister(self, process: subprocess.Popen) -> None:
        with self.lock:
            if process in self.processes:
                self.processes.remove(process)

    def stop(self) -> None:
        self.cancel.set()
        with self.lock:
            processes = list(self.processes)
        for process in processes:
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass


_ACTIVE_LOCK = threading.Lock()
_ACTIVE: JobController | None = None


def cancel_active_job() -> str:
    global _ACTIVE
    if _ACTIVE is None:
        return "No render is running."
    _ACTIVE.stop()
    return "Stop requested; partial output will be removed and diagnostics retained."


def _run_json(command: list[str]) -> dict:
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Media probe failed")
    return json.loads(result.stdout)


def probe_video(path: str | os.PathLike[str]) -> dict:
    # Быстрый анализ: сначала метаданные (мгновенно), -count_frames (декодирует ВСЕ кадры)
    # только если nb_frames не указан — иначе 4K-файл на 10+ минут «висит» на анализе.
    data = _run_json(
        [
            str(FFPROBE),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=index,codec_name,width,height,avg_frame_rate,r_frame_rate,time_base,duration,nb_frames,nb_read_frames,color_primaries,color_transfer,color_space:stream_tags=rotate:stream_side_data=rotation",
            "-show_entries",
            "format=duration,format_name",
            "-of",
            "json",
            str(path),
        ]
    )
    streams = data.get("streams") or []
    if not streams:
        raise ValueError("The selected file contains no decodable video stream.")
    stream = streams[0]
    rotation = int((stream.get("tags") or {}).get("rotate", 0) or 0)
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            rotation = int(side["rotation"] or 0)
    rotation %= 360
    width, height = int(stream["width"]), int(stream["height"])
    if rotation in (90, 270):
        width, height = height, width
    frames = int(stream.get("nb_read_frames") or stream.get("nb_frames") or 0)
    if frames <= 0:
        # Нет точного числа кадров в метаданных — считаем через -count_frames (медленно, но точно)
        counted = _run_json(
            [
                str(FFPROBE),
                "-v",
                "error",
                "-count_frames",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=nb_read_frames",
                "-of",
                "json",
                str(path),
            ]
        )
        frames = int((counted.get("streams") or [{}])[0].get("nb_read_frames") or 0)
    if frames <= 0:
        raise ValueError("Could not determine an exact frame count for this video.")
    rate_text = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "30/1"
    rate = Fraction(rate_text) if rate_text != "0/0" else Fraction(30, 1)
    transfer = stream.get("color_transfer") or "unknown"
    return {
        "width": width,
        "height": height,
        "coded_width": int(stream["width"]),
        "coded_height": int(stream["height"]),
        "rotation": rotation,
        "frames": frames,
        "fps": float(rate),
        "rate": rate,
        "time_base": Fraction(stream.get("time_base") or "1/1000"),
        "duration": float((data.get("format") or {}).get("duration") or stream.get("duration") or 0),
        "codec": stream.get("codec_name") or "unknown",
        "format": (data.get("format") or {}).get("format_name") or "unknown",
        "color_transfer": transfer,
        "hdr": transfer in {"smpte2084", "arib-std-b67"},
    }


def detect_gpu() -> dict:
    command = [
        "nvidia-smi",
        "--query-gpu=name,driver_version,memory.total,compute_cap",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("NVIDIA driver tools are unavailable; an RTX GPU and current driver are required.") from exc
    candidates = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 4 and "RTX" in parts[0].upper():
            candidates.append(parts)
    if not candidates:
        raise RuntimeError("No supported NVIDIA RTX GPU was detected.")
    name, driver, memory, capability = candidates[0]
    match = re.search(r"RTX\s+(\d{2})", name.upper())
    generation = int(match.group(1)) if match else 0
    if generation < 30:
        raise RuntimeError(f"{name} is outside the supported RTX 30/40/50 scope.")
    return {
        "name": name,
        "driver": driver,
        "memory_mb": int(memory),
        "compute_capability": capability,
        "generation": generation,
        "beta": generation == 30,
    }


def resolve_backend() -> str:
    """Select an explicit backend without ever mislabeling software output as DLSS."""
    requested = os.environ.get("DLSS5_BACKEND", "auto").strip().lower()
    if requested not in {"auto", "dlss", "relay", "gpu", "software"}:
        raise RuntimeError("DLSS5_BACKEND must be one of: auto, dlss, relay, gpu, software")
    runtime_ready = os.name == "nt" and WORKER.is_file() and (RUNTIME / "nvngx_dlssnr.dll").is_file()
    if requested == "auto":
        if os.environ.get("DLSS5_RELAY_URL"):
            return "relay"
        return "dlss" if runtime_ready else "software"
    if requested == "dlss" and not runtime_ready:
        raise RuntimeError(
            "The DLSS backend requires Windows plus bin/runtime/nvngx.dll and "
            "bin/runtime/nvngx_dlssnr.dll. Linux containers cannot execute this D3D12 worker."
        )
    if requested == "relay":
        if not os.environ.get("DLSS5_RELAY_URL") or not os.environ.get("DLSS5_RELAY_TOKEN"):
            raise RuntimeError("The relay backend requires DLSS5_RELAY_URL and DLSS5_RELAY_TOKEN")
    if requested == "gpu":
        opencl_gpu_info()
    return requested


class RelayWorker:
    """File-like adapter for a worker hosted by the Windows relay."""

    def __init__(self, connection: socket.socket):
        self.connection = connection
        self.stdin = connection.makefile("wb", buffering=0)
        self.stdout = connection.makefile("rb", buffering=0)
        self._closed = False

    @classmethod
    def connect(cls) -> "RelayWorker":
        relay_url = os.environ["DLSS5_RELAY_URL"]
        host, separator, port_text = relay_url.rpartition(":")
        if not separator or not host:
            raise RuntimeError("DLSS5_RELAY_URL must use host:port format")
        token = os.environ["DLSS5_RELAY_TOKEN"].encode("utf-8")
        if not 16 <= len(token) <= 4096:
            raise RuntimeError("DLSS5_RELAY_TOKEN must contain 16 to 4096 UTF-8 bytes")
        connection = socket.create_connection((host, int(port_text)), timeout=15)
        connection.settimeout(None)
        connection.sendall(b"D5R1" + struct.pack("!H", len(token)) + token)
        reply = _recv_exact(connection, 2)
        if reply != b"OK":
            connection.close()
            reason = "busy" if reply == b"BS" else "authentication rejected"
            raise RuntimeError(f"Windows DLSS relay connection failed: {reason}")
        return cls(connection)

    def poll(self):
        return 0 if self._closed else None

    def terminate(self):
        if not self._closed:
            self._closed = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()

    def finish(self) -> tuple[int, list[str]]:
        self.stdin.flush()
        self.connection.shutdown(socket.SHUT_WR)
        footer = _read_exact(self.stdout, 12)
        magic, return_code, log_size = struct.unpack("!4siI", footer)
        if magic != b"D5LF" or log_size > 8 * 1024 * 1024:
            raise RuntimeError("Invalid response footer from Windows DLSS relay")
        logs = _read_exact(self.stdout, log_size).decode("utf-8", "replace").splitlines()
        self._closed = True
        self.stdout.close()
        self.stdin.close()
        self.connection.close()
        return return_code, logs


def _software_process(rgba: np.ndarray, settings: dict) -> np.ndarray:
    """Portable CPU preview filter. This is intentionally not presented as DLSS."""
    rgb = rgba[..., :3].astype(np.float32)
    sigma = 0.8 + float(settings["local_structure"]) * 0.7
    blurred = cv2.GaussianBlur(rgb, (0, 0), sigmaX=sigma, sigmaY=sigma)
    amount = 0.18 + float(settings["intensity"]) * 0.22
    sharpened = cv2.addWeighted(rgb, 1.0 + amount, blurred, -amount, 0)
    contrast = 1.0 + (float(settings["local_tone"]) - 1.0) * 0.08
    sharpened = (sharpened - 127.5) * contrast + 127.5
    result = rgba.copy()
    result[..., :3] = np.clip(sharpened, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(result)


def opencl_gpu_info() -> dict:
    if not cv2.ocl.haveOpenCL():
        raise RuntimeError(
            "The GPU preview backend requires an OpenCL runtime inside the container. "
            "Install NVIDIA Container Toolkit and start with compose.gpu.yaml."
        )
    cv2.ocl.setUseOpenCL(True)
    # Force creation of the OpenCL context before querying its default device.
    cv2.add(cv2.UMat(np.zeros((2, 2), dtype=np.uint8)), 1).get()
    if not cv2.ocl.useOpenCL():
        raise RuntimeError("OpenCV found OpenCL but could not activate an OpenCL device.")
    device = cv2.ocl.Device_getDefault()
    device_type = int(device.type())
    gpu_type = int(getattr(cv2.ocl, "Device_TYPE_GPU", 4))
    if not device.available() or not (device_type & gpu_type):
        raise RuntimeError("The selected OpenCL device is not a GPU; refusing a silent CPU fallback.")
    return {
        "name": device.name() or "OpenCL GPU",
        "driver": device.driverVersion() or "OpenCL",
        "vendor": device.vendorName() or "unknown",
        "memory_mb": int(device.globalMemSize()) // (1024 * 1024),
        "compute_capability": device.OpenCL_C_Version() or "OpenCL",
        "generation": 0,
        "beta": False,
    }


def _gpu_process(rgba: np.ndarray, settings: dict) -> np.ndarray:
    """OpenCL T-API implementation of the portable preview filter."""
    if not cv2.ocl.useOpenCL():
        opencl_gpu_info()
    rgb = cv2.UMat(rgba[..., :3].astype(np.float32))
    sigma = 0.8 + float(settings["local_structure"]) * 0.7
    blurred = cv2.GaussianBlur(rgb, (0, 0), sigmaX=sigma, sigmaY=sigma)
    amount = 0.18 + float(settings["intensity"]) * 0.22
    sharpened = cv2.addWeighted(rgb, 1.0 + amount, blurred, -amount, 0)
    contrast = 1.0 + (float(settings["local_tone"]) - 1.0) * 0.08
    adjusted = cv2.addWeighted(sharpened, contrast, sharpened, 0.0, 127.5 * (1.0 - contrast))
    cv2.ocl.finish()
    result = rgba.copy()
    result[..., :3] = np.clip(adjusted.get(), 0, 255).astype(np.uint8)
    return np.ascontiguousarray(result)


def _native_settings(options: ConversionOptions) -> dict:
    settings = PROFILES.get(options.profile)
    if settings is None:
        raise RuntimeError(f"Unknown native DLSS 5 profile: {options.profile}")
    settings = dict(settings)
    overrides = {
        "intensity": options.intensity,
        "local_tone": options.local_tone,
        "local_structure": options.local_structure,
        "skin_structure": options.skin_structure,
    }
    settings.update({key: value for key, value in overrides.items() if value is not None})
    return settings


def _backend_gpu(backend: str) -> dict:
    if backend == "dlss":
        return detect_gpu()
    if backend == "gpu":
        return opencl_gpu_info()
    return {
        "name": "Windows DLSS relay" if backend == "relay" else "Portable CPU software backend",
        "driver": "n/a",
        "memory_mb": 0,
        "compute_capability": "n/a",
        "generation": 0,
        "beta": False,
    }


def _pipeline_name(backend: str) -> str:
    return {
        "dlss": "direct-dlssnr-feature18",
        "relay": "windows-relay-dlssnr-feature18",
        "gpu": "portable-opencl-gpu-preview",
        "software": "portable-cpu-software-preview",
    }[backend]


def resolve_size(metadata: dict, options: ConversionOptions) -> tuple[int, int]:
    return int(metadata["width"]), int(metadata["height"])


def _resize_fit(rgba: np.ndarray, width: int, height: int) -> np.ndarray:
    source_h, source_w = rgba.shape[:2]
    scale = min(width / source_w, height / source_h)
    fit_w = max(1, min(width, int(round(source_w * scale))))
    fit_h = max(1, min(height, int(round(source_h * scale))))
    resized = cv2.resize(rgba, (fit_w, fit_h), interpolation=cv2.INTER_LANCZOS4)
    canvas = np.zeros((height, width, 4), dtype=np.uint8)
    canvas[..., 3] = 255
    x = (width - fit_w) // 2
    y = (height - fit_h) // 2
    canvas[y : y + fit_h, x : x + fit_w] = resized
    return canvas


def _rotate(frame: np.ndarray, rotation: int) -> np.ndarray:
    if rotation == 90:
        return np.ascontiguousarray(np.rot90(frame, 3))
    if rotation == 180:
        return np.ascontiguousarray(np.rot90(frame, 2))
    if rotation == 270:
        return np.ascontiguousarray(np.rot90(frame, 1))
    return frame


def _read_exact(stream, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        block = stream.read(size - len(chunks))
        if not block:
            raise EOFError(f"Native worker stopped after {len(chunks)} of {size} output bytes")
        chunks.extend(block)
    return bytes(chunks)


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        block = connection.recv(size - len(chunks))
        if not block:
            raise EOFError("Relay closed during handshake")
        chunks.extend(block)
    return bytes(chunks)


def _drain_text(stream, lines: list[str]) -> None:
    try:
        for raw in iter(stream.readline, b""):
            lines.append(raw.decode("utf-8", "replace").rstrip())
    finally:
        stream.close()


def _encoder_probe(codec: str) -> bool:
    command = [
        str(FFMPEG), "-v", "error", "-f", "lavfi", "-i", "color=size=256x256:rate=1",
        "-frames:v", "1", "-c:v", codec, "-f", "null", "-",
    ]
    return subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode == 0


def _codec_command(options: ConversionOptions) -> tuple[list[str], str]:
    cq = {"High": "17", "Balanced": "20", "Small": "24"}[options.quality]
    if options.codec == "H.264":
        if _encoder_probe("h264_nvenc"):
            return ["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", cq, "-b:v", "0", "-pix_fmt", "yuv420p"], "h264_nvenc"
        return ["-c:v", "libx264", "-preset", "slow", "-crf", cq, "-pix_fmt", "yuv420p"], "libx264"
    if options.codec == "HEVC":
        if _encoder_probe("hevc_nvenc"):
            return ["-c:v", "hevc_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", cq, "-b:v", "0", "-pix_fmt", "yuv420p"], "hevc_nvenc"
        return ["-c:v", "libx265", "-preset", "slow", "-crf", cq, "-pix_fmt", "yuv420p"], "libx265"
    if not _encoder_probe("av1_nvenc"):
        raise RuntimeError("AV1 NVENC is not supported by this GPU/driver. Choose H.264 or HEVC.")
    return ["-c:v", "av1_nvenc", "-preset", "p6", "-rc", "vbr", "-cq", cq, "-b:v", "0", "-pix_fmt", "yuv420p"], "av1_nvenc"


def _start_encoder(temp_video: Path, options: ConversionOptions, controller: JobController):
    codec_args, selected = _codec_command(options)
    command = [
        str(FFMPEG), "-hide_banner", "-loglevel", "warning", "-y", "-f", "nut", "-i", "pipe:0",
        "-map", "0:v:0", "-an", *codec_args, "-fps_mode", "passthrough", str(temp_video),
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    controller.register(process)
    logs: list[str] = []
    thread = threading.Thread(target=_drain_text, args=(process.stderr, logs), daemon=True)
    thread.start()
    return process, thread, logs, selected


def _final_mux(temp_video: Path, source: Path, output: Path, options: ConversionOptions) -> None:
    if options.container == "MKV":
        maps = ["-map", "0:v:0", "-map", "1:a?", "-map", "1:s?"]
        streams = ["-c:v", "copy", "-c:a", "copy", "-c:s", "copy"]
        # MKV: без -shortest — аудио-копия просто закончится раньше, это нормально
        extra = []
    else:
        maps = ["-map", "0:v:0", "-map", "1:a?"]
        streams = ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
        # MP4: apad дотягивает аудио тишиной до конца видео, -shortest режет по видео
        # (иначе при аудио короче видео на доли секунды теряются кадры хвоста)
        extra = ["-af", "apad", "-shortest"]
    command = [
        str(FFMPEG), "-hide_banner", "-loglevel", "warning", "-y", "-i", str(temp_video), "-i", str(source),
        *maps, "-map_metadata", "1", "-map_chapters", "1", *streams, *extra, str(output),
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError("Final audio/metadata mux failed:\n" + result.stderr[-4000:])


def compute_video_metrics(source_path: str | os.PathLike[str], output_path: str | os.PathLike[str], step: int = 30) -> dict:
    """PSNR/SSIM between source and final output on every step-th frame.

    Never raises: any failure returns {} so metrics cannot break the render.
    """
    try:
        source_container = av.open(str(source_path))
        output_container = av.open(str(output_path))
        try:
            source_stream = source_container.streams.video[0]
            output_stream = output_container.streams.video[0]
            psnr_sum = 0.0
            ssim_sum = 0.0
            samples = 0
            for index, (src_frame, out_frame) in enumerate(
                zip(source_container.decode(source_stream), output_container.decode(output_stream))
            ):
                if index % step != 0:
                    continue
                src = src_frame.to_ndarray(format="rgb24")
                out = out_frame.to_ndarray(format="rgb24")
                if src.shape != out.shape:
                    out = cv2.resize(out, (src.shape[1], src.shape[0]), interpolation=cv2.INTER_LANCZOS4)
                src_f = src.astype(np.float64) / 255.0
                out_f = out.astype(np.float64) / 255.0
                mse = float(np.mean((src_f - out_f) ** 2))
                psnr = float("inf") if mse == 0.0 else 20.0 * np.log10(1.0 / np.sqrt(mse))
                mu_a = src_f.mean()
                mu_b = out_f.mean()
                var_a = src_f.var()
                var_b = out_f.var()
                cov = float(np.mean((src_f - mu_a) * (out_f - mu_b)))
                c1 = 0.01 ** 2
                c2 = 0.03 ** 2
                ssim = ((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / ((mu_a ** 2 + mu_b ** 2 + c1) * (var_a + var_b + c2))
                psnr_sum += psnr
                ssim_sum += ssim
                samples += 1
            if samples == 0:
                return {}
            return {
                "psnr": round(psnr_sum / samples, 2),
                "ssim": round(ssim_sum / samples, 4),
                "samples": samples,
            }
        finally:
            source_container.close()
            output_container.close()
    except Exception:
        return {}


def convert_video(
    input_path: str | os.PathLike[str],
    options: ConversionOptions | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> ConversionResult:
    global _ACTIVE
    options = options or ConversionOptions()
    source = Path(input_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if options.preserve_hdr:
        raise RuntimeError("HDR preservation is disabled in this build because the verified DLSSNR path is RGBA8. HDR input is converted to SDR instead of being mislabeled as HDR.")
    backend = resolve_backend()
    required = [FFMPEG, FFPROBE]
    if backend == "dlss":
        required.extend([WORKER, RUNTIME / "nvngx_dlssnr.dll"])
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError("Portable runtime is incomplete:\n" + "\n".join(missing))

    controller = JobController()
    if not _ACTIVE_LOCK.acquire(blocking=False):
        raise RuntimeError("Another GPU render is already running.")
    _ACTIVE = controller
    started = time.perf_counter()
    job_dir: Path | None = None
    output: Path | None = None
    try:
        if progress:
            progress(0.0, "Анализ видео: декодирование кадров (ffprobe)...")
        metadata = probe_video(source)
        gpu = _backend_gpu(backend)
        if progress:
            label = "feature 18" if backend in {"dlss", "relay"} else ("OpenCL GPU preview (not DLSS)" if backend == "gpu" else "portable software preview (not DLSS)")
            progress(0.005, f"Видео: {metadata['width']}x{metadata['height']}, {metadata['frames']} кадров — запуск {label}...")
        width, height = resolve_size(metadata, options)
        OUTPUTS.mkdir(exist_ok=True)
        JOBS.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000:06d}"
        job_dir = JOBS / f"{source.stem}-{stamp}-{os.getpid()}"
        job_dir.mkdir(parents=True, exist_ok=False)
        extension = ".mkv" if options.container == "MKV" else ".mp4"
        output_marker = "DLSS5" if backend in {"dlss", "relay"} else ("GPU" if backend == "gpu" else "SOFTWARE")
        output = OUTPUTS / f"{source.stem}_{output_marker}_{stamp}{extension}"
        ORIGINALS.mkdir(exist_ok=True)
        original_path = ORIGINALS / f"{source.stem}_ORIGINAL_{stamp}{source.suffix}"
        shutil.copy2(source, original_path)
        temp_video = job_dir / "processed-video.mkv"
        native = _native_settings(options)
        worker_logs: list[str] = []
        worker = None
        worker_thread = None
        if backend in {"dlss", "relay"}:
            if progress:
                progress(0.01, f"Starting feature 18 on {gpu['name']}")
            if backend == "relay":
                worker = RelayWorker.connect()
            else:
                creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                worker = subprocess.Popen(
                    [str(WORKER), "--video"], cwd=RUNTIME, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, creationflags=creation_flags,
                )
            controller.register(worker)
            if backend == "dlss":
                worker_thread = threading.Thread(target=_drain_text, args=(worker.stderr, worker_logs), daemon=True)
                worker_thread.start()
            header = struct.pack(
                "<10I4f", VIDEO_MAGIC, width, height, int(options.warmup_frames), int(metadata["frames"]),
                native["profile"], native["preset"], native["style"], native["auto_mask"], native["ui_correction"],
                native["intensity"], native["local_tone"], native["local_structure"], native["skin_structure"],
            )
            worker.stdin.write(header)
            worker.stdin.flush()

        encoder, encoder_thread, encoder_logs, selected_encoder = _start_encoder(temp_video, options, controller)
        nut = av.open(encoder.stdin, mode="w", format="nut")
        input_container = av.open(str(source))
        input_stream = input_container.streams.video[0]
        input_stream.thread_type = "AUTO"
        rate = input_stream.average_rate or metadata["rate"]
        raw_stream = nut.add_stream("rawvideo", rate=rate)
        raw_stream.width = width
        raw_stream.height = height
        raw_stream.pix_fmt = "rgba"
        raw_stream.time_base = input_stream.time_base or metadata["time_base"]
        guides = TemporalGuideGenerator(width, height) if backend in {"dlss", "relay"} else None
        delivered = 0
        scene_resets = 0
        for index, frame in enumerate(input_container.decode(input_stream)):
            if controller.cancel.is_set():
                raise Cancelled("Render stopped by user.")
            rgba = frame.to_ndarray(format="rgba")
            rgba = _rotate(rgba, metadata["rotation"])
            if rgba.shape[1] != width or rgba.shape[0] != height:
                rgba = _resize_fit(rgba, width, height)
            rgba = np.ascontiguousarray(rgba, dtype=np.uint8)
            pts = int(frame.pts if frame.pts is not None else index)
            if backend in {"dlss", "relay"}:
                guide = guides.process(rgba)
                scene_resets += int(guide.reset and index != 0)
                frame_header = struct.pack("<4Iq", FRAME_MAGIC, index, int(guide.reset), 0, pts)
                worker.stdin.write(frame_header)
                worker.stdin.write(rgba.tobytes())
                worker.stdin.write(guide.motion.tobytes())
                worker.stdin.flush()
                result_header = _read_exact(worker.stdout, struct.calcsize("<5Iq"))
                magic, out_index, ok, byte_count, ngx_result, out_pts = struct.unpack("<5Iq", result_header)
                if magic != OUT_MAGIC or not ok or out_index != index or byte_count != width * height * 4:
                    raise RuntimeError(f"Invalid native worker response for frame {index}")
                if ngx_result != 1:
                    raise RuntimeError(f"Direct feature-18 evaluation failed on frame {index}: 0x{ngx_result:08X}")
                processed = np.frombuffer(_read_exact(worker.stdout, byte_count), dtype=np.uint8).reshape(height, width, 4)
            elif backend == "gpu":
                processed = _gpu_process(rgba, native)
                out_pts = pts
            else:
                processed = _software_process(rgba, native)
                out_pts = pts
            out_frame = av.VideoFrame.from_ndarray(processed, format="rgba")
            out_frame.pts = out_pts
            out_frame.time_base = input_stream.time_base or metadata["time_base"]
            for packet in raw_stream.encode(out_frame):
                nut.mux(packet)
            delivered += 1
            if progress:
                frame_label = "DLSS 5" if backend in {"dlss", "relay"} else ("GPU" if backend == "gpu" else "Software")
                progress(0.04 + 0.84 * delivered / metadata["frames"], f"{frame_label} frame {delivered}/{metadata['frames']}")

        if delivered != metadata["frames"]:
            raise RuntimeError(f"Decoded {delivered} frames but probe reported {metadata['frames']}; refusing an incomplete render.")
        for packet in raw_stream.encode():
            nut.mux(packet)
        nut.close()
        if encoder.stdin and not encoder.stdin.closed:
            encoder.stdin.close()
        input_container.close()
        if worker is not None:
            if backend == "relay":
                worker_code, worker_logs = worker.finish()
            else:
                worker.stdin.close()
                worker_code = worker.wait(timeout=60)
                worker_thread.join(timeout=2)
            controller.unregister(worker)
            if worker_code:
                raise RuntimeError("Native DLSS worker failed:\n" + "\n".join(worker_logs[-40:]))
        encoder_code = encoder.wait(timeout=120)
        encoder_thread.join(timeout=2)
        controller.unregister(encoder)
        if encoder_code:
            raise RuntimeError("Video encoder failed:\n" + "\n".join(encoder_logs[-40:]))

        nr_count = delivered if backend in {"dlss", "relay"} else 0
        direct_create_result = None
        if backend in {"dlss", "relay"}:
            create_matches = re.findall(r"direct feature 18 ready:.*result=0x([0-9A-Fa-f]{8})", "\n".join(worker_logs))
            if not create_matches:
                raise RuntimeError("Direct feature-18 creation result was not reported; refusing unverifiable output.")
            direct_create_result = f"0x{create_matches[-1].upper()}"
        if progress:
            progress(0.91, "Muxing original audio and metadata")
        _final_mux(temp_video, source, output, options)
        metrics = compute_video_metrics(source, output)
        verified = probe_video(output)
        if verified["frames"] != delivered:
            raise RuntimeError(f"Output verification found {verified['frames']} frames instead of {delivered}.")

        elapsed = time.perf_counter() - started
        report = {
            "status": "success",
            "media_type": "video",
            "input": str(source),
            "output": str(output),
            "original_path": str(original_path),
            "metrics": metrics,
            "options": asdict(options),
            "input_metadata": {key: str(value) if isinstance(value, Fraction) else value for key, value in metadata.items()},
            "output_metadata": {key: str(value) if isinstance(value, Fraction) else value for key, value in verified.items()},
            "gpu": gpu,
            "encoder": selected_encoder,
            "frames_processed": delivered,
            "scene_resets": scene_resets,
            "backend": backend,
            "pipeline": _pipeline_name(backend),
            "feature_id": 18 if backend in {"dlss", "relay"} else None,
            "feature_18_confirmed": backend in {"dlss", "relay"},
            "direct_create_result": direct_create_result,
            "successful_direct_evaluations": nr_count,
            "model_sha256": hashlib.sha256((RUNTIME / "nvngx_dlssnr.dll").read_bytes()).hexdigest() if backend == "dlss" else None,
            "worker_sha256": hashlib.sha256(WORKER.read_bytes()).hexdigest() if backend == "dlss" else None,
            "runtime_location": "windows-relay" if backend == "relay" else "local",
            "native_settings": native,
            "elapsed_seconds": elapsed,
            "average_fps": delivered / elapsed,
            "worker_log": worker_logs,
            "encoder_log": encoder_logs,
        }
        report_path = output.with_suffix(output.suffix + ".report.json")
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        if progress:
            message = "Complete — feature 18 confirmed" if backend in {"dlss", "relay"} else ("Complete — OpenCL GPU preview (not DLSS)" if backend == "gpu" else "Complete — software preview (not DLSS)")
            progress(1.0, message)
        return ConversionResult(str(output), str(report_path), delivered, nr_count, elapsed, gpu["name"], backend)
    except Exception as exc:
        was_cancelled = controller.cancel.is_set()
        controller.stop()
        if output and output.exists():
            output.unlink()
        if was_cancelled and not isinstance(exc, Cancelled):
            raise Cancelled("Render stopped by user.") from exc
        raise
    finally:
        _ACTIVE = None
        _ACTIVE_LOCK.release()
        if job_dir and job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)


def _decode_image(path: Path) -> np.ndarray:
    max_bytes = int(os.environ.get("DLSS5_MAX_IMAGE_BYTES", str(256 * 1024**2)))
    if path.stat().st_size > max_bytes:
        raise ValueError(f"Image exceeds the configured {max_bytes:,}-byte limit.")
    encoded = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
    if image is None or image.size == 0:
        raise ValueError("The selected file is not a decodable PNG, JPEG, or WebP image.")
    if image.dtype == np.uint16:
        image = (image / 257).astype(np.uint8)
    elif image.dtype != np.uint8:
        raise ValueError(f"Unsupported image sample type: {image.dtype}")
    if image.ndim == 2:
        rgba = cv2.cvtColor(image, cv2.COLOR_GRAY2RGBA)
    elif image.ndim == 3 and image.shape[2] == 3:
        rgba = cv2.cvtColor(image, cv2.COLOR_BGR2RGBA)
    elif image.ndim == 3 and image.shape[2] == 4:
        rgba = cv2.cvtColor(image, cv2.COLOR_BGRA2RGBA)
    else:
        raise ValueError("Unsupported image channel layout.")
    height, width = rgba.shape[:2]
    max_pixels = int(os.environ.get("DLSS5_MAX_IMAGE_PIXELS", "100000000"))
    if width * height > max_pixels:
        raise ValueError(f"Image exceeds the configured {max_pixels:,}-pixel limit.")
    return np.ascontiguousarray(rgba)


def _write_png(path: Path, rgba: np.ndarray) -> None:
    bgra = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA)
    ok, encoded = cv2.imencode(".png", bgra, [cv2.IMWRITE_PNG_COMPRESSION, 3])
    if not ok:
        raise RuntimeError("OpenCV could not encode the processed image as PNG.")
    path.write_bytes(encoded.tobytes())


def _image_metrics(source: np.ndarray, output: np.ndarray) -> dict:
    source_rgb = source[..., :3].astype(np.float64) / 255.0
    output_rgb = output[..., :3].astype(np.float64) / 255.0
    mse = float(np.mean((source_rgb - output_rgb) ** 2))
    psnr = float("inf") if mse == 0.0 else 20.0 * np.log10(1.0 / np.sqrt(mse))
    return {
        "psnr": None if not np.isfinite(psnr) else round(psnr, 2),
        "mean_absolute_change": round(float(np.mean(np.abs(source_rgb - output_rgb))), 6),
    }


def convert_image(
    input_path: str | os.PathLike[str],
    options: ConversionOptions | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> ConversionResult:
    """Process a static image and emit a lossless PNG result."""
    global _ACTIVE
    options = options or ConversionOptions()
    source = Path(input_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError("Image input must be PNG, JPEG, or WebP.")
    backend = resolve_backend()
    if backend == "dlss":
        missing = [str(path) for path in (WORKER, RUNTIME / "nvngx_dlssnr.dll") if not path.exists()]
        if missing:
            raise RuntimeError("Portable runtime is incomplete:\n" + "\n".join(missing))

    controller = JobController()
    if not _ACTIVE_LOCK.acquire(blocking=False):
        raise RuntimeError("Another GPU render is already running.")
    _ACTIVE = controller
    started = time.perf_counter()
    output: Path | None = None
    worker = None
    worker_thread = None
    worker_logs: list[str] = []
    try:
        if progress:
            progress(0.0, "Decoding image...")
        rgba = _decode_image(source)
        height, width = rgba.shape[:2]
        if backend in {"dlss", "relay"} and (width < 64 or height < 64 or width > 7680 or height > 4320):
            raise ValueError("DLSS image dimensions must be between 64x64 and 7680x4320.")
        gpu = _backend_gpu(backend)
        settings = _native_settings(options)
        OUTPUTS.mkdir(exist_ok=True)
        ORIGINALS.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000:06d}"
        output_marker = "DLSS5" if backend in {"dlss", "relay"} else ("GPU" if backend == "gpu" else "SOFTWARE")
        output = OUTPUTS / f"{source.stem}_{output_marker}_{stamp}.png"
        original_path = ORIGINALS / f"{source.stem}_ORIGINAL_{stamp}{source.suffix.lower()}"
        shutil.copy2(source, original_path)

        if controller.cancel.is_set():
            raise Cancelled("Render stopped by user.")
        if backend in {"dlss", "relay"}:
            if progress:
                progress(0.1, f"Starting feature 18 on {gpu['name']}")
            if backend == "relay":
                worker = RelayWorker.connect()
            else:
                worker = subprocess.Popen(
                    [str(WORKER), "--video"], cwd=RUNTIME, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            controller.register(worker)
            if backend == "dlss":
                worker_thread = threading.Thread(target=_drain_text, args=(worker.stderr, worker_logs), daemon=True)
                worker_thread.start()
            header = struct.pack(
                "<10I4f", VIDEO_MAGIC, width, height, int(options.warmup_frames), 1,
                settings["profile"], settings["preset"], settings["style"], settings["auto_mask"], settings["ui_correction"],
                settings["intensity"], settings["local_tone"], settings["local_structure"], settings["skin_structure"],
            )
            motion = np.zeros((height, width, 2), dtype=np.float16)
            worker.stdin.write(header)
            worker.stdin.write(struct.pack("<4Iq", FRAME_MAGIC, 0, 1, 0, 0))
            worker.stdin.write(rgba.tobytes())
            worker.stdin.write(motion.tobytes())
            worker.stdin.flush()
            result_header = _read_exact(worker.stdout, struct.calcsize("<5Iq"))
            magic, out_index, ok, byte_count, ngx_result, _ = struct.unpack("<5Iq", result_header)
            if magic != OUT_MAGIC or not ok or out_index != 0 or byte_count != width * height * 4:
                raise RuntimeError("Invalid native worker response for image")
            if ngx_result != 1:
                raise RuntimeError(f"Direct feature-18 image evaluation failed: 0x{ngx_result:08X}")
            processed = np.frombuffer(_read_exact(worker.stdout, byte_count), dtype=np.uint8).reshape(height, width, 4).copy()
            if backend == "relay":
                worker_code, worker_logs = worker.finish()
            else:
                worker.stdin.close()
                worker_code = worker.wait(timeout=60)
                worker_thread.join(timeout=2)
            controller.unregister(worker)
            if worker_code:
                raise RuntimeError("Native DLSS worker failed:\n" + "\n".join(worker_logs[-40:]))
            create_matches = re.findall(r"direct feature 18 ready:.*result=0x([0-9A-Fa-f]{8})", "\n".join(worker_logs))
            if not create_matches:
                raise RuntimeError("Direct feature-18 creation result was not reported; refusing unverifiable output.")
            direct_create_result = f"0x{create_matches[-1].upper()}"
        elif backend == "gpu":
            if progress:
                progress(0.2, "Processing image with OpenCL GPU preview (not DLSS)...")
            processed = _gpu_process(rgba, settings)
            direct_create_result = None
        else:
            if progress:
                progress(0.2, "Processing image with portable software preview (not DLSS)...")
            processed = _software_process(rgba, settings)
            direct_create_result = None

        if controller.cancel.is_set():
            raise Cancelled("Render stopped by user.")
        if progress:
            progress(0.9, "Writing lossless PNG...")
        _write_png(output, processed)
        verified = _decode_image(output)
        if verified.shape != processed.shape:
            raise RuntimeError("Output image verification found unexpected dimensions or channels.")
        elapsed = time.perf_counter() - started
        nr_count = 1 if backend in {"dlss", "relay"} else 0
        report = {
            "status": "success",
            "media_type": "image",
            "input": str(source),
            "output": str(output),
            "original_path": str(original_path),
            "metrics": _image_metrics(rgba, verified),
            "options": asdict(options),
            "input_metadata": {"width": width, "height": height, "channels": 4, "format": source.suffix.lower().lstrip(".")},
            "output_metadata": {"width": width, "height": height, "channels": 4, "format": "png"},
            "gpu": gpu,
            "encoder": "png-lossless",
            "frames_processed": 1,
            "scene_resets": 0,
            "backend": backend,
            "pipeline": _pipeline_name(backend),
            "feature_id": 18 if backend in {"dlss", "relay"} else None,
            "feature_18_confirmed": backend in {"dlss", "relay"},
            "direct_create_result": direct_create_result,
            "successful_direct_evaluations": nr_count,
            "model_sha256": hashlib.sha256((RUNTIME / "nvngx_dlssnr.dll").read_bytes()).hexdigest() if backend == "dlss" else None,
            "worker_sha256": hashlib.sha256(WORKER.read_bytes()).hexdigest() if backend == "dlss" else None,
            "runtime_location": "windows-relay" if backend == "relay" else "local",
            "native_settings": settings,
            "elapsed_seconds": elapsed,
            "worker_log": worker_logs,
        }
        report_path = output.with_suffix(output.suffix + ".report.json")
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        if progress:
            message = "Complete — feature 18 confirmed" if nr_count else ("Complete — OpenCL GPU preview (not DLSS)" if backend == "gpu" else "Complete — software preview (not DLSS)")
            progress(1.0, message)
        return ConversionResult(str(output), str(report_path), 1, nr_count, elapsed, gpu["name"], backend, "image")
    except Exception as exc:
        was_cancelled = controller.cancel.is_set()
        controller.stop()
        if output and output.exists():
            output.unlink()
        if was_cancelled and not isinstance(exc, Cancelled):
            raise Cancelled("Render stopped by user.") from exc
        raise
    finally:
        _ACTIVE = None
        _ACTIVE_LOCK.release()


def convert_media(
    input_path: str | os.PathLike[str],
    options: ConversionOptions | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> ConversionResult:
    suffix = Path(input_path).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return convert_image(input_path, options, progress)
    if suffix in VIDEO_SUFFIXES:
        return convert_video(input_path, options, progress)
    raise ValueError("Supported inputs are MP4, MKV, WebM, PNG, JPEG, and WebP.")
