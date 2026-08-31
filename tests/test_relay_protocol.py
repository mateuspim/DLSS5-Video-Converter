import os
import socket
import struct
import threading
import unittest
from unittest.mock import patch

from dlss5_converter.core import RelayWorker


def recv_exact(connection, size):
    data = bytearray()
    while len(data) < size:
        block = connection.recv(size - len(data))
        if not block:
            raise EOFError
        data.extend(block)
    return bytes(data)


class RelayProtocolTests(unittest.TestCase):
    def test_authenticated_handshake_and_log_footer(self):
        token = b"0123456789abcdef"
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        host, port = listener.getsockname()

        def fake_relay():
            connection, _ = listener.accept()
            with connection:
                self.assertEqual(recv_exact(connection, 4), b"D5R1")
                token_size = struct.unpack("!H", recv_exact(connection, 2))[0]
                self.assertEqual(recv_exact(connection, token_size), token)
                connection.sendall(b"OK")
                while connection.recv(4096):
                    pass
                logs = b"direct feature 18 ready: result=0x00000001\n"
                connection.sendall(struct.pack("!4siI", b"D5LF", 0, len(logs)) + logs)
            listener.close()

        thread = threading.Thread(target=fake_relay, daemon=True)
        thread.start()
        environment = {
            "DLSS5_RELAY_URL": f"{host}:{port}",
            "DLSS5_RELAY_TOKEN": token.decode(),
        }
        with patch.dict(os.environ, environment):
            worker = RelayWorker.connect()
            return_code, logs = worker.finish()
        thread.join(timeout=2)
        self.assertEqual(return_code, 0)
        self.assertIn("direct feature 18 ready", logs[0])


if __name__ == "__main__":
    unittest.main()
