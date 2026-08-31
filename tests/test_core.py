import json
import os
import tempfile
import unittest
from pathlib import Path
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

    def test_explicit_gpu_backend_requires_and_reports_opencl_gpu(self):
        gpu = {
            "name": "Mock RTX OpenCL",
            "driver": "test",
            "vendor": "NVIDIA",
            "memory_mb": 1024,
            "compute_capability": "OpenCL C 3.0",
            "generation": 0,
            "beta": False,
        }
        with patch.dict(os.environ, {"DLSS5_BACKEND": "gpu"}), patch.object(
            core, "opencl_gpu_info", return_value=gpu
        ):
            self.assertEqual(core.resolve_backend(), "gpu")

    def test_image_conversion_writes_lossless_png_and_report(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "input.png"
            rgba = np.zeros((48, 64, 4), dtype=np.uint8)
            rgba[..., 0] = np.arange(64, dtype=np.uint8)[None, :]
            rgba[..., 1] = np.arange(48, dtype=np.uint8)[:, None]
            rgba[..., 2] = 120
            rgba[..., 3] = 173
            core._write_png(source, rgba)
            with patch.dict(os.environ, {"DLSS5_BACKEND": "software"}), patch.multiple(
                core,
                OUTPUTS=base / "outputs",
                ORIGINALS=base / "originals",
            ):
                result = core.convert_media(source, core.ConversionOptions(profile="Natural"))

            output = Path(result.output_path)
            decoded = core._decode_image(output)
            report = json.loads(Path(result.report_path).read_text())
            self.assertEqual(result.media_type, "image")
            self.assertEqual(output.suffix, ".png")
            self.assertEqual(decoded.shape, rgba.shape)
            np.testing.assert_array_equal(decoded[..., 3], rgba[..., 3])
            self.assertEqual(report["media_type"], "image")
            self.assertFalse(report["feature_18_confirmed"])
            self.assertEqual(report["encoder"], "png-lossless")

    def test_gpu_image_conversion_is_labeled_not_dlss(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "input.png"
            rgba = np.full((48, 64, 4), 127, dtype=np.uint8)
            rgba[..., 3] = 255
            core._write_png(source, rgba)
            gpu = {
                "name": "Mock RTX OpenCL", "driver": "test", "vendor": "NVIDIA",
                "memory_mb": 1024, "compute_capability": "OpenCL C 3.0", "generation": 0, "beta": False,
            }
            with patch.dict(os.environ, {"DLSS5_BACKEND": "gpu"}), patch.multiple(
                core,
                OUTPUTS=base / "outputs",
                ORIGINALS=base / "originals",
            ), patch.object(core, "opencl_gpu_info", return_value=gpu), patch.object(
                core, "_gpu_process", side_effect=lambda image, settings: image.copy()
            ):
                result = core.convert_image(source)
            report = json.loads(Path(result.report_path).read_text())
            self.assertEqual(result.backend, "gpu")
            self.assertIn("_GPU_", Path(result.output_path).name)
            self.assertEqual(report["pipeline"], "portable-opencl-gpu-preview")
            self.assertFalse(report["feature_18_confirmed"])


if __name__ == "__main__":
    unittest.main()
