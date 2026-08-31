import os
import unittest
from unittest.mock import patch

import numpy as np

from dlss5_converter import core


class BackendTests(unittest.TestCase):
    def test_explicit_software_backend(self):
        with patch.dict(os.environ, {"DLSS5_BACKEND": "software"}):
            self.assertEqual(core.resolve_backend(), "software")

    def test_invalid_backend_is_rejected(self):
        with patch.dict(os.environ, {"DLSS5_BACKEND": "magic"}):
            with self.assertRaisesRegex(RuntimeError, "DLSS5_BACKEND"):
                core.resolve_backend()

    def test_relay_requires_url_and_token(self):
        with patch.dict(os.environ, {"DLSS5_BACKEND": "relay"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "DLSS5_RELAY_URL"):
                core.resolve_backend()

    def test_auto_selects_configured_relay(self):
        environment = {
            "DLSS5_BACKEND": "auto",
            "DLSS5_RELAY_URL": "host.docker.internal:17861",
            "DLSS5_RELAY_TOKEN": "0123456789abcdef",
        }
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(core.resolve_backend(), "relay")

    def test_software_filter_preserves_shape_type_and_alpha(self):
        frame = np.zeros((24, 32, 4), dtype=np.uint8)
        frame[..., :3] = np.arange(32, dtype=np.uint8)[None, :, None]
        frame[..., 3] = 173
        result = core._software_process(frame, core.PROFILES["Natural"])
        self.assertEqual(result.shape, frame.shape)
        self.assertEqual(result.dtype, np.uint8)
        np.testing.assert_array_equal(result[..., 3], frame[..., 3])


if __name__ == "__main__":
    unittest.main()
