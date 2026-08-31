from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(slots=True)
class GuideFrame:
    motion: np.ndarray
    reset: bool
    scene_score: float


class TemporalGuideGenerator:
    """Estimate the guide buffers an encoded video does not contain."""

    def __init__(self, width: int, height: int, flow_width: int = 640) -> None:
        self.width = width
        self.height = height
        scale = min(1.0, flow_width / width)
        self.flow_width = max(64, int(round(width * scale / 2) * 2))
        self.flow_height = max(64, int(round(height * scale / 2) * 2))
        self.previous_gray: np.ndarray | None = None
        self.dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
        self.dis.setUseSpatialPropagation(True)
        self.dis.setFinestScale(1)

    def _small_gray(self, rgba: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(rgba, cv2.COLOR_RGBA2GRAY)
        return cv2.resize(gray, (self.flow_width, self.flow_height), interpolation=cv2.INTER_AREA)

    def process(self, rgba: np.ndarray) -> GuideFrame:
        current = self._small_gray(rgba)
        pixels = self.width * self.height
        if self.previous_gray is None:
            motion = np.zeros((self.height, self.width, 2), dtype=np.float32)
            reset = True
            scene_score = 1.0
        else:
            scene_score = float(np.mean(cv2.absdiff(current, self.previous_gray))) / 255.0
            reset = scene_score > 0.24
            if reset:
                motion = np.zeros((self.height, self.width, 2), dtype=np.float32)
            else:
                # NGX consumes current-to-previous motion in pixel units.
                cur_to_prev = self.dis.calc(current, self.previous_gray, None)
                prev_to_cur = self.dis.calc(self.previous_gray, current, None)
                yy, xx = np.mgrid[0 : self.flow_height, 0 : self.flow_width].astype(np.float32)
                sample_x = xx + cur_to_prev[..., 0]
                sample_y = yy + cur_to_prev[..., 1]
                reverse = cv2.remap(
                    prev_to_cur,
                    sample_x,
                    sample_y,
                    cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_CONSTANT,
                )
                consistency = cv2.magnitude(
                    cur_to_prev[..., 0] + reverse[..., 0],
                    cur_to_prev[..., 1] + reverse[..., 1],
                )
                warped_previous = cv2.remap(
                    self.previous_gray,
                    sample_x,
                    sample_y,
                    cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_REFLECT,
                )
                residual = cv2.absdiff(current, warped_previous).astype(np.float32) / 255.0
                invalid = np.maximum(np.clip(consistency / 2.5, 0.0, 1.0), np.clip(residual * 4.0, 0.0, 1.0))
                invalid = cv2.dilate(invalid, np.ones((3, 3), np.uint8), iterations=1)

                motion = cv2.resize(cur_to_prev, (self.width, self.height), interpolation=cv2.INTER_LINEAR)
                motion[..., 0] *= self.width / self.flow_width
                motion[..., 1] *= self.height / self.flow_height
        self.previous_gray = current
        assert motion.size == pixels * 2
        return GuideFrame(
            motion=np.ascontiguousarray(motion.astype(np.float16)),
            reset=reset,
            scene_score=scene_score,
        )
