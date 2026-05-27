"""Per-image "direct sun" detection.

A pixel is "directly lit" if it's bright AND lives in a region with hard
shadow edges. The calibrated Pi exposure (99th-percentile V <= 250) leaves
only ~30 levels of headroom between bright-diffuse-lit surfaces and direct-sun
pixels, so a plain V threshold catches white walls and overcast sky too. The
local-variance gate fixes that: direct sun makes hard shadow edges (high local
variance); diffuse light doesn't.
"""
from __future__ import annotations

import cv2
import numpy as np


def sun_mask(rgb: np.ndarray, t_v: int = 220, variance_window: int = 9) -> np.ndarray:
    """Per-pixel binary mask of directly-lit regions.

    Steps:
      1. V channel = max(R, G, B) (HSV definition of V).
      2. Threshold V > t_v.
      3. Dilate the bright mask, then AND with a "hard-edges-here" mask
         derived from local V variance (75th percentile of frame variance).

    Returns a uint8 array (H, W) of {0, 255}.
    """
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError(f"Expected H x W x 3 RGB array, got shape {rgb.shape}")

    # HSV's V == per-pixel max across the three channels. Use cv2.cvtColor so
    # we share OpenCV's well-tested path even though V == rgb.max(2) here.
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    v = hsv[:, :, 2]  # uint8

    bright = (v > t_v).astype(np.uint8) * 255

    # Step 3: dilate a few pixels so the variance test, which lives at the
    # edges of bright patches, has overlap with the bright mask itself.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    bright_dilated = cv2.dilate(bright, kernel, iterations=1)

    # Local variance via E[X^2] - E[X]^2 over a boxFilter window. Float math
    # to avoid uint8 wrap on the square term.
    v_f = v.astype(np.float32)
    mean = cv2.boxFilter(v_f, ddepth=cv2.CV_32F, ksize=(variance_window, variance_window))
    mean_sq = cv2.boxFilter(v_f * v_f, ddepth=cv2.CV_32F, ksize=(variance_window, variance_window))
    local_var = np.maximum(mean_sq - mean * mean, 0.0)

    # Self-tuning per frame: top quartile of local variance counts as "hard
    # edges here." A scene-independent variance threshold would either miss
    # subtle shadows or get fooled by flat textured walls.
    var_thresh = float(np.percentile(local_var, 75))
    hard_edges = (local_var > var_thresh).astype(np.uint8) * 255

    mask = cv2.bitwise_and(bright_dilated, hard_edges)
    return mask
