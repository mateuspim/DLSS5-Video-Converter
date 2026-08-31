import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from dlss5_converter import core


def recv_exact(connection, size):
    data = bytearray()
    while len(data) < size:
        block = connection.recv(size - len(data))
        if not block:
            raise EOFError
        data.extend(block)
    return bytes(data)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
class RelayEndToEndTests(unittest.TestCase):
    def test_video_pipeline_through_mock_windows_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "input.mp4"
            subprocess.run(
                [
                    shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=6",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                    "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(source),
                ],
                check=True,
            )
            token = b"0123456789abcdef"
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            host, port = listener.getsockname()

            def mock_worker():
                connection, _ = listener.accept()
                with connection:
                    self.assertEqual(recv_exact(connection, 4), b"D5R1")
                    token_size = struct.unpack("!H", recv_exact(connection, 2))[0]
                    self.assertEqual(recv_exact(connection, token_size), token)
                    connection.sendall(b"OK")
                    video_header = struct.unpack("<10I4f", recv_exact(connection, struct.calcsize("<10I4f")))
                    width, height, frame_count = video_header[1], video_header[2], video_header[4]
                    pixel_bytes = width * height * 4
                    for _ in range(frame_count):
                        frame_header = recv_exact(connection, struct.calcsize("<4Iq"))
                        _, index, _, _, pts = struct.unpack("<4Iq", frame_header)
                        rgba = recv_exact(connection, pixel_bytes)
                        recv_exact(connection, pixel_bytes)  # float16 two-channel motion vectors
                        response = struct.pack("<5Iq", core.OUT_MAGIC, index, 1, pixel_bytes, 1, pts)
                        connection.sendall(response + rgba)
                    while connection.recv(4096):
                        pass
                    logs = b"direct feature 18 ready: result=0x00000001\n"
                    connection.sendall(struct.pack("!4siI", b"D5LF", 0, len(logs)) + logs)
                listener.close()

            thread = threading.Thread(target=mock_worker, daemon=True)
            thread.start()
            environment = {
                "DLSS5_BACKEND": "relay",
                "DLSS5_RELAY_URL": f"{host}:{port}",
                "DLSS5_RELAY_TOKEN": token.decode(),
            }
            with patch.dict(os.environ, environment), patch.multiple(
                core,
                OUTPUTS=base / "outputs",
                JOBS=base / "jobs",
                ORIGINALS=base / "originals",
            ):
                result = core.convert_video(source, core.ConversionOptions(quality="Small"))
            thread.join(timeout=2)
            report = json.loads(Path(result.report_path).read_text())
            self.assertEqual(result.backend, "relay")
            self.assertEqual(result.frames, 6)
            self.assertTrue(report["feature_18_confirmed"])
            self.assertEqual(report["pipeline"], "windows-relay-dlssnr-feature18")
            self.assertEqual(report["successful_direct_evaluations"], 6)


if __name__ == "__main__":
    unittest.main()
