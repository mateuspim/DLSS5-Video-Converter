#!/usr/bin/env python3
"""Authenticated TCP bridge from a Linux container to the Windows DLSS worker."""

from __future__ import annotations

import argparse
import hmac
import os
import socket
import socketserver
import struct
import subprocess
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "bin" / "runtime"
WORKER = RUNTIME / "nvngx.dll"
ACTIVE = threading.Lock()
MAX_LOG_BYTES = 8 * 1024 * 1024


def read_exact(stream, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        block = stream.read(size - len(data))
        if not block:
            raise EOFError("connection closed during relay handshake")
        data.extend(block)
    return bytes(data)


def drain_logs(stream, destination: bytearray) -> None:
    while True:
        block = stream.read(64 * 1024)
        if not block:
            return
        destination.extend(block)
        if len(destination) > MAX_LOG_BYTES:
            del destination[:-MAX_LOG_BYTES]


class RelayHandler(socketserver.BaseRequestHandler):
    token: bytes = b""

    def handle(self) -> None:
        connection: socket.socket = self.request
        connection.settimeout(20)
        incoming = connection.makefile("rb", buffering=0)
        acquired = False
        try:
            magic = read_exact(incoming, 4)
            token_size = struct.unpack("!H", read_exact(incoming, 2))[0]
            supplied = read_exact(incoming, token_size) if token_size <= 4096 else b""
            if magic != b"D5R1" or not hmac.compare_digest(supplied, self.token):
                connection.sendall(b"NO")
                return
            if not ACTIVE.acquire(blocking=False):
                connection.sendall(b"BS")
                return
            acquired = True
            connection.sendall(b"OK")
            connection.settimeout(None)
            self.run_worker(connection, incoming)
        except (EOFError, OSError, struct.error):
            return
        finally:
            if acquired:
                ACTIVE.release()
            incoming.close()

    def run_worker(self, connection: socket.socket, incoming) -> None:
        process = subprocess.Popen(
            [str(WORKER), "--video"],
            cwd=RUNTIME,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        logs = bytearray()

        def feed_worker() -> None:
            try:
                while True:
                    block = incoming.read(1024 * 1024)
                    if not block:
                        break
                    process.stdin.write(block)
                    process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass

        input_thread = threading.Thread(target=feed_worker, daemon=True)
        log_thread = threading.Thread(target=drain_logs, args=(process.stderr, logs), daemon=True)
        input_thread.start()
        log_thread.start()
        try:
            while True:
                block = process.stdout.read1(1024 * 1024)
                if not block:
                    break
                connection.sendall(block)
            return_code = process.wait(timeout=60)
            input_thread.join(timeout=2)
            log_thread.join(timeout=2)
            connection.sendall(struct.pack("!4siI", b"D5LF", return_code, len(logs)) + logs)
        except (BrokenPipeError, ConnectionError, OSError, subprocess.TimeoutExpired):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


class RelayServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=17861)
    args = parser.parse_args()
    token = os.environ.get("DLSS5_RELAY_TOKEN", "").encode("utf-8")
    if len(token) < 16:
        raise SystemExit("Set DLSS5_RELAY_TOKEN to a secret containing at least 16 UTF-8 bytes.")
    missing = [path for path in (WORKER, RUNTIME / "nvngx_dlssnr.dll") if not path.is_file()]
    if missing:
        raise SystemExit("Missing Windows runtime files:\n" + "\n".join(map(str, missing)))
    RelayHandler.token = token
    with RelayServer((args.host, args.port), RelayHandler) as server:
        print(f"DLSS relay listening on {args.host}:{args.port}")
        server.serve_forever()


if __name__ == "__main__":
    main()
