# Third-party components

The application is distributed as an experimental local build. Refer to each upstream project for its full license and redistribution terms.

| Component | Version/source | SHA-256 of staged runtime |
| --- | --- | --- |
| User-provided modified DLSSNR model | 310.8.0 approximate FP16 | `DCC0DC2414AEDEC4A8E084647070383BE068554042587180C20C784D4772D36F` |
| Standalone direct feature-18 worker (`nvngx.dll`) | Adapted from MIT DLSS5-Feeder host | `99EF1F2976D9CD16B7FC269ADB6C6450FB64C81A522C9B9E6EDC6A28201DC904` |

DLSS5-Feeder's MIT license is in `native/DLSS5-Feeder/LICENSE`. NVIDIA's SDK license is in `native/NVIDIA-DLSS/LICENSE.txt`. FFmpeg licensing information is in `bin/ffmpeg/LICENSE`.

The model hash is informational only. Its modified signature is intentionally not used as a runtime rejection condition. ReShade, RenoDX, DLSS Super Resolution carrier binaries, test media, and generated outputs are not included.
