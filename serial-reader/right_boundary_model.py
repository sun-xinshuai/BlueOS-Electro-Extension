#!/usr/bin/env python3
"""Small pure-Python model for right-wall distance estimation.

The training side can use NumPy, but the deployed predictor intentionally has
no third-party dependency so it also works in the existing BlueOS image.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Sequence


FEATURE_NAMES = (
    tuple("ch%d_amp_mv" % i for i in range(8))
    + tuple("ch%d_norm" % i for i in range(8))
    + ("right_minus_reference_mv", "right_reference_ratio", "amp_mean_mv", "amp_std_mv")
)


def build_feature_vector(channel_amp_mv: Sequence[float]) -> List[float]:
    """Build the model features from the same recommended FFT amplitudes used by the UI."""
    amps = [float(v) for v in channel_amp_mv]
    if len(amps) != 8:
        raise ValueError("right-wall model requires 8 channel amplitudes")

    mean_amp = sum(amps) / 8.0
    scale = max(abs(mean_amp), 1e-6)
    normalized = [value / scale for value in amps]
    right = (amps[4] + amps[5]) / 2.0
    reference = (amps[0] + amps[1] + amps[2] + amps[3] + amps[6] + amps[7]) / 6.0
    ratio = right / max(abs(reference), 1e-6)
    variance = sum((value - mean_amp) ** 2 for value in amps) / 8.0
    return amps + normalized + [right - reference, ratio, mean_amp, math.sqrt(variance)]


class RightBoundaryModel:
    """Standardized linear ridge model loaded from a JSON artifact."""

    def __init__(self, payload: Dict[str, object]):
        names = payload.get("feature_names")
        if list(names or []) != list(FEATURE_NAMES):
            raise ValueError("unsupported right-wall model feature schema")
        self.feature_names = list(FEATURE_NAMES)
        self.mean = [float(v) for v in payload["feature_mean"]]
        self.scale = [max(abs(float(v)), 1e-9) for v in payload["feature_scale"]]
        self.weights = [float(v) for v in payload["weights"]]
        self.intercept = float(payload["intercept"])
        self.min_distance_cm = float(payload.get("min_distance_cm", 0.0))
        self.max_distance_cm = float(payload.get("max_distance_cm", 0.0))
        self.rmse_cm = float(payload.get("rmse_cm", 0.0))
        self.sample_count = int(payload.get("sample_count", 0))
        if not (len(self.mean) == len(self.scale) == len(self.weights) == len(FEATURE_NAMES)):
            raise ValueError("invalid right-wall model dimensions")

    @classmethod
    def load(cls, path: Path) -> "RightBoundaryModel":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def predict(self, channel_amp_mv: Sequence[float]) -> Dict[str, object]:
        features = build_feature_vector(channel_amp_mv)
        normalized = [(x - m) / s for x, m, s in zip(features, self.mean, self.scale)]
        distance = self.intercept + sum(w * x for w, x in zip(self.weights, normalized))
        clipped = max(self.min_distance_cm, min(self.max_distance_cm, distance))
        in_range = self.min_distance_cm <= distance <= self.max_distance_cm
        distance_confidence = 1.0 / (1.0 + max(self.rmse_cm, 0.0) / 5.0)
        if not in_range:
            distance_confidence *= 0.5
        return {
            "side": "right",
            "side_label": "右侧",
            "distance_cm": round(clipped, 2),
            "raw_distance_cm": round(distance, 2),
            "valid": True,
            "in_training_range": in_range,
            "confidence": round(distance_confidence, 3),
            "model_rmse_cm": round(self.rmse_cm, 2),
            "sample_count": self.sample_count,
        }


def load_optional(path: Path) -> Optional[RightBoundaryModel]:
    try:
        if not Path(path).exists():
            return None
        return RightBoundaryModel.load(path)
    except Exception:
        return None
