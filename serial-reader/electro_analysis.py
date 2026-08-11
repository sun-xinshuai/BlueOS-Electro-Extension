#!/usr/bin/env python3
"""Active electro-sensing analysis for real BlueOS serial data.

The analyzer keeps a fixed-size 1 s window at 100 Hz, extracts the 20 Hz
response with a narrow-band FFT-like estimator, and supports a captured null
baseline for boundary-distance inversion.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


SAMPLE_RE = re.compile(
    r"^\[(\d\d):(\d\d):(\d\d)\.(\d{3})\]\s+"
    r"SEQ:(\d+)\s+"
    r"(-?\d+),(-?\d+),(-?\d+),(-?\d+),(-?\d+),(-?\d+),(-?\d+),(-?\d+)\s+"
    r"OK:(\d+)\s+DROP:(\d+)\s+OVERRUN:(\d+)"
)


def _counts_to_mv(count: float, adc_full_scale_mv: float) -> float:
    return float(count) * adc_full_scale_mv / 32768.0


def _lockin_amplitude_mv(samples_mv: Sequence[float], freq_hz: float, fs_hz: float) -> float:
    """Return the sinusoidal peak amplitude at freq_hz using lock-in detection."""
    n = len(samples_mv)
    if n == 0:
        return 0.0
    mean = sum(samples_mv) / float(n)
    s = 0.0
    c = 0.0
    w = 2.0 * math.pi * freq_hz / fs_hz
    for i, x in enumerate(samples_mv):
        x = float(x) - mean
        ang = w * i
        s += x * math.sin(ang)
        c += x * math.cos(ang)
    s = 2.0 * s / n
    c = 2.0 * c / n
    return math.sqrt(s * s + c * c)


def _dft_band(samples_mv: Sequence[float], fs_hz: float, bins: Sequence[int]) -> Tuple[List[float], List[float]]:
    """Compute a narrow-band DFT magnitude on integer-frequency bins."""
    n = len(samples_mv)
    if n == 0:
        return [], []
    mean = sum(samples_mv) / float(n)
    centered = [float(x) - mean for x in samples_mv]
    freqs = []
    amps = []
    for bin_k in bins:
        f = float(bin_k) * fs_hz / float(n)
        w = 2.0 * math.pi * f / fs_hz
        s = 0.0
        c = 0.0
        for i, x in enumerate(centered):
            ang = w * i
            s += x * math.sin(ang)
            c += x * math.cos(ang)
        s = 2.0 * s / n
        c = 2.0 * c / n
        freqs.append(f)
        amps.append(math.sqrt(s * s + c * c))
    return freqs, amps


def _parabolic_peak(freqs: Sequence[float], amps: Sequence[float], idx: int) -> Tuple[float, float]:
    if idx <= 0 or idx >= len(amps) - 1:
        return float(freqs[idx]), float(amps[idx])
    y0 = float(amps[idx - 1])
    y1 = float(amps[idx])
    y2 = float(amps[idx + 1])
    denom = y0 - 2.0 * y1 + y2
    if abs(denom) < 1e-12:
        return float(freqs[idx]), y1
    delta = 0.5 * (y0 - y2) / denom
    bin_width = float(freqs[1] - freqs[0]) if len(freqs) > 1 else 0.0
    peak_freq = float(freqs[idx] + delta * bin_width)
    peak_amp = float(y1 - 0.25 * (y0 - y2) * delta)
    return peak_freq, peak_amp


class ElectroAnalyzer:
    def __init__(
        self,
        driver,
        baseline_path: Path,
        sample_rate_hz: float = 100.0,
        target_hz: float = 20.0,
        window_size: int = 100,
        step_size: int = 10,
        adc_full_scale_mv: float = 10000.0,
        conductivity_uS_cm: float = 800.0,
        model_a_mv: float = 12.932008,
        model_l_cm: float = 34.834192,
    ):
        self.driver = driver
        self.baseline_path = Path(baseline_path)
        self.sample_rate_hz = float(sample_rate_hz)
        self.target_hz = float(target_hz)
        self.window_size = int(window_size)
        self.step_size = int(step_size)
        self.adc_full_scale_mv = float(adc_full_scale_mv)
        self.conductivity_uS_cm = float(conductivity_uS_cm)
        self.model_a_mv = float(model_a_mv)
        self.model_l_cm = float(model_l_cm)

        self.ref_idx = [0, 1, 2, 3, 6, 7]
        self.right_idx = [4, 5]

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._last_history_index = 0
        self._sample_buffer = deque(maxlen=max(self.window_size * 4, 400))
        self._samples_since_compute = 0
        self._last_seq = None

        self._history = deque(maxlen=240)

        self._baseline_channels_mv = None
        self._baseline_feature_mv = None
        self._baseline_saved_at = None
        self._baseline_source = None

        self._capture_mode = None
        self._capture_windows = []

        self._state = self._empty_state()
        self.load_baseline()
        self._sync_baseline_state()

    def _sync_baseline_state(self) -> None:
        self._state["baseline_ready"] = self._baseline_feature_mv is not None
        self._state["baseline_feature_mv"] = self._baseline_feature_mv
        self._state["baseline_saved_at"] = self._baseline_saved_at
        self._state["baseline_source"] = self._baseline_source
        if self._baseline_channels_mv is not None:
            self._state["baseline_channel_amp_mv"] = list(self._baseline_channels_mv)
        else:
            self._state["baseline_channel_amp_mv"] = None

    def _empty_state(self) -> Dict[str, object]:
        return {
            "ready": False,
            "sample_rate_hz": self.sample_rate_hz,
            "target_hz": self.target_hz,
            "window_size": self.window_size,
            "conductivity_uS_cm": self.conductivity_uS_cm,
            "baseline_ready": self._baseline_feature_mv is not None,
            "baseline_feature_mv": self._baseline_feature_mv,
            "baseline_saved_at": self._baseline_saved_at,
            "baseline_source": self._baseline_source,
            "capturing_null": False,
            "null_progress": 0.0,
            "null_windows_collected": 0,
            "null_windows_target": 0,
            "peak_freq_hz": None,
            "peak_amp_mv": None,
            "target_amp_mv": None,
            "raw_feature_mv": None,
            "delta_feature_mv": None,
            "estimated_distance_cm": None,
            "channel_amp_mv": [None] * 8,
            "channel_delta_mv": [None] * 8,
            "history": [],
            "last_seq": None,
            "seq_range": None,
            "window_timestamp": None,
            "model_a_mv": self.model_a_mv,
            "model_l_cm": self.model_l_cm,
        }

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def load_baseline(self) -> bool:
        if not self.baseline_path.exists():
            return False
        try:
            payload = json.loads(self.baseline_path.read_text(encoding="utf-8"))
            with self._lock:
                self._baseline_channels_mv = payload.get("channel_amp_mv")
                self._baseline_feature_mv = payload.get("feature_mv")
                self._baseline_saved_at = payload.get("captured_at")
                self._baseline_source = payload.get("source", "file")
                self._sync_baseline_state()
            return True
        except Exception:
            return False

    def _save_baseline(self) -> None:
        payload = {
            "captured_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "source": "captured",
            "sample_rate_hz": self.sample_rate_hz,
            "target_hz": self.target_hz,
            "window_size": self.window_size,
            "conductivity_uS_cm": self.conductivity_uS_cm,
            "model_a_mv": self.model_a_mv,
            "model_l_cm": self.model_l_cm,
            "channel_amp_mv": self._baseline_channels_mv,
            "feature_mv": self._baseline_feature_mv,
        }
        self.baseline_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def clear_baseline(self) -> None:
        with self._lock:
            self._baseline_channels_mv = None
            self._baseline_feature_mv = None
            self._baseline_saved_at = None
            self._baseline_source = None
            self._capture_mode = None
            self._capture_windows = []
            try:
                if self.baseline_path.exists():
                    self.baseline_path.unlink()
            except Exception:
                pass
            self._state = self._empty_state()

    def reset_stream(self) -> None:
        with self._lock:
            self._last_history_index = 0
            self._sample_buffer.clear()
            self._samples_since_compute = 0
            self._last_seq = None
            self._history.clear()
            self._capture_mode = None
            self._capture_windows = []
            self._state["capturing_null"] = False
            self._state["null_progress"] = 0.0
            self._state["null_windows_collected"] = 0
            self._state["null_windows_target"] = 0
            self._state["history"] = []

    def start_null_capture(self, seconds: float = 8.0) -> Dict[str, object]:
        seconds = max(1.0, float(seconds))
        target_windows = max(10, int(seconds * self.sample_rate_hz / float(self.step_size)))
        with self._lock:
            self._capture_mode = {
                "target_windows": target_windows,
                "captured_at": time.time(),
            }
            self._capture_windows = []
            self._state["capturing_null"] = True
            self._state["null_progress"] = 0.0
            self._state["null_windows_collected"] = 0
            self._state["null_windows_target"] = target_windows
        return self.get_state()

    def get_state(self) -> Dict[str, object]:
        with self._lock:
            state = dict(self._state)
            if self._baseline_channels_mv is not None:
                state["baseline_channel_amp_mv"] = list(self._baseline_channels_mv)
            else:
                state["baseline_channel_amp_mv"] = None
            state["history"] = list(self._history)
            return state

    def _append_history(self, snapshot: Dict[str, object]) -> None:
        entry = {
            "ts": time.time(),
            "distance_cm": snapshot.get("estimated_distance_cm"),
            "delta_feature_mv": snapshot.get("delta_feature_mv"),
            "raw_feature_mv": snapshot.get("raw_feature_mv"),
            "peak_freq_hz": snapshot.get("peak_freq_hz"),
        }
        self._history.append(entry)
        snapshot["history"] = list(self._history)

    def _parse_sample(self, raw_line: str) -> Optional[Dict[str, object]]:
        match = SAMPLE_RE.match(raw_line.strip())
        if not match:
            return None
        hh, mm, ss, ms = map(int, match.group(1, 2, 3, 4))
        seq = int(match.group(5))
        counts = [int(x) for x in match.group(6, 7, 8, 9, 10, 11, 12, 13)]
        return {
            "seq": seq,
            "ts": ((hh * 60 + mm) * 60 + ss) + ms / 1000.0,
            "counts": counts,
        }

    def ingest_history(self, entries: Sequence[Dict[str, object]]) -> None:
        for entry in entries:
            raw = entry.get("raw", "")
            sample = self._parse_sample(raw)
            if sample is None:
                continue
            seq = sample["seq"]
            if self._last_seq is not None and seq != self._last_seq + 1:
                self._sample_buffer.clear()
                self._samples_since_compute = 0
            self._last_seq = seq
            self._sample_buffer.append(sample)
            self._samples_since_compute += 1
            if len(self._sample_buffer) >= self.window_size and self._samples_since_compute >= self.step_size:
                self._samples_since_compute = 0
                self._compute_state()

    def _compute_channel_amplitudes(self, window_mv: Sequence[Sequence[float]]) -> Dict[str, object]:
        n = len(window_mv)
        if n == 0:
            return {}
        target_bin = int(round(self.target_hz * n / self.sample_rate_hz))
        start_bin = max(1, target_bin - 5)
        end_bin = min(n // 2, target_bin + 5)
        bins = list(range(start_bin, end_bin + 1))
        freqs = [b * self.sample_rate_hz / float(n) for b in bins]

        channel_amp_mv = []
        channel_peak_freq_hz = []
        channel_peak_amp_mv = []
        channel_target_amp_mv = []
        channel_delta_mv = []

        baseline = self._baseline_channels_mv
        for ch in range(8):
            samples = [float(row[ch]) for row in window_mv]
            amps = _dft_band(samples, self.sample_rate_hz, bins)[1]
            peak_idx = max(range(len(amps)), key=lambda i: amps[i])
            peak_freq_hz, peak_amp_mv = _parabolic_peak(freqs, amps, peak_idx)
            recommended_amp_mv = _lockin_amplitude_mv(samples, peak_freq_hz, self.sample_rate_hz)
            target_amp_mv = _lockin_amplitude_mv(samples, self.target_hz, self.sample_rate_hz)
            channel_amp_mv.append(recommended_amp_mv)
            channel_peak_freq_hz.append(peak_freq_hz)
            channel_peak_amp_mv.append(peak_amp_mv)
            channel_target_amp_mv.append(target_amp_mv)
            if baseline is None:
                channel_delta_mv.append(None)
            else:
                channel_delta_mv.append(recommended_amp_mv - float(baseline[ch]))

        raw_feature_mv = float(sum(channel_amp_mv[i] for i in self.right_idx) / len(self.right_idx) -
                               sum(channel_amp_mv[i] for i in self.ref_idx) / len(self.ref_idx))
        baseline_feature_mv = self._baseline_feature_mv if self._baseline_feature_mv is not None else 0.0
        delta_feature_mv = raw_feature_mv - baseline_feature_mv
        estimated_distance_cm = None
        if self._baseline_feature_mv is not None and delta_feature_mv > 1e-9:
            estimated_distance_cm = -self.model_l_cm * math.log(delta_feature_mv / self.model_a_mv)

        peak_index = max(range(len(channel_amp_mv)), key=lambda i: channel_amp_mv[i])
        peak_freq_hz = channel_peak_freq_hz[peak_index]
        peak_amp_mv = channel_amp_mv[peak_index]
        target_amp_mv = float(sum(channel_target_amp_mv) / len(channel_target_amp_mv))

        return {
            "channel_amp_mv": channel_amp_mv,
            "channel_peak_freq_hz": channel_peak_freq_hz,
            "channel_peak_amp_mv": channel_peak_amp_mv,
            "channel_target_amp_mv": channel_target_amp_mv,
            "channel_delta_mv": channel_delta_mv,
            "raw_feature_mv": raw_feature_mv,
            "delta_feature_mv": delta_feature_mv,
            "estimated_distance_cm": estimated_distance_cm,
            "peak_freq_hz": peak_freq_hz,
            "peak_amp_mv": peak_amp_mv,
            "target_amp_mv": target_amp_mv,
        }

    def _compute_state(self) -> None:
        window = list(self._sample_buffer)[-self.window_size :]
        window_mv = [[_counts_to_mv(v, self.adc_full_scale_mv) for v in row["counts"]] for row in window]
        feats = self._compute_channel_amplitudes(window_mv)
        if not feats:
            return

        with self._lock:
            if self._capture_mode is not None:
                self._capture_windows.append(feats["channel_amp_mv"])
                captured = len(self._capture_windows)
                target = int(self._capture_mode["target_windows"])
                self._state["capturing_null"] = True
                self._state["null_windows_collected"] = captured
                self._state["null_windows_target"] = target
                self._state["null_progress"] = min(1.0, float(captured) / float(target))
                if captured >= target:
                    baseline = [0.0] * 8
                    for ch in range(8):
                        baseline[ch] = sum(window_amp[ch] for window_amp in self._capture_windows) / float(captured)
                    self._baseline_channels_mv = baseline
                    self._baseline_feature_mv = (
                        sum(baseline[i] for i in self.right_idx) / len(self.right_idx)
                        - sum(baseline[i] for i in self.ref_idx) / len(self.ref_idx)
                    )
                    self._baseline_saved_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                    self._baseline_source = "captured"
                    self._save_baseline()
                    self._capture_mode = None
                    self._capture_windows = []
                    self._state["capturing_null"] = False
                    self._state["null_progress"] = 1.0
                    self._state["baseline_ready"] = True
                    self._state["baseline_feature_mv"] = self._baseline_feature_mv
                    self._state["baseline_saved_at"] = self._baseline_saved_at
                    self._state["baseline_source"] = self._baseline_source
            else:
                self._state["capturing_null"] = False
                self._state["null_progress"] = 0.0

            self._state.update({
                "ready": self._baseline_feature_mv is not None,
                "baseline_ready": self._baseline_feature_mv is not None,
                "baseline_feature_mv": self._baseline_feature_mv,
                "baseline_saved_at": self._baseline_saved_at,
                "baseline_source": self._baseline_source,
                "peak_freq_hz": round(feats["peak_freq_hz"], 3),
                "peak_amp_mv": round(feats["peak_amp_mv"], 3),
                "target_amp_mv": round(feats["target_amp_mv"], 3),
                "raw_feature_mv": round(feats["raw_feature_mv"], 4),
                "delta_feature_mv": round(feats["delta_feature_mv"], 4),
                "estimated_distance_cm": None if feats["estimated_distance_cm"] is None else round(feats["estimated_distance_cm"], 2),
                "channel_amp_mv": [round(v, 3) for v in feats["channel_amp_mv"]],
                "channel_delta_mv": [None if v is None else round(v, 3) for v in feats["channel_delta_mv"]],
                "window_timestamp": window[-1]["ts"],
                "last_seq": window[-1]["seq"],
                "seq_range": [window[0]["seq"], window[-1]["seq"]],
            })
            self._append_history(dict(self._state))

    def _loop(self) -> None:
        self._last_history_index = getattr(self.driver, "total_lines", 0)
        while not self._stop.is_set():
            try:
                entries = self.driver.get_history_since(self._last_history_index, limit=2000)
                if entries:
                    self._last_history_index = int(entries[-1]["index"])
                    self.ingest_history(entries)
            except Exception:
                pass
            time.sleep(0.08)
