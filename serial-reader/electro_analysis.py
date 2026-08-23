#!/usr/bin/env python3
"""Active electro-sensing FFT analysis for real BlueOS serial data.

The analyzer keeps a fixed-size 1 s window at 100 Hz, extracts the 20 Hz
response with a narrow-band FFT-like estimator, and supports a captured null
baseline plus trajectory display.
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
    r"^(?:\[(?P<hh>\d\d):(?P<mm>\d\d):(?P<ss>\d\d)\.(?P<ms>\d{3})\]\s+)?"
    r"SEQ:(?P<seq>\d+)\s+"
    r"(?P<ch0>-?\d+),(?P<ch1>-?\d+),(?P<ch2>-?\d+),(?P<ch3>-?\d+),"
    r"(?P<ch4>-?\d+),(?P<ch5>-?\d+),(?P<ch6>-?\d+),(?P<ch7>-?\d+)\s+"
    r"OK:(?P<ok>\d+)"
    r"(?:\s+POS:(?P<pos_id>-?\d+),(?P<pos_x>-?\d+),(?P<pos_y>-?\d+),(?P<pos_z>-?\d+)"
    r"\s+PV:(?P<pos_valid>\d+)\s+PSEQ:(?P<pos_seq>\d+))?"
    r"\s+DROP:(?P<drop>\d+)\s+OVERRUN:(?P<overrun>\d+)"
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
    ):
        self.driver = driver
        self.baseline_path = Path(baseline_path)
        self.sample_rate_hz = float(sample_rate_hz)
        self.target_hz = float(target_hz)
        self.window_size = int(window_size)
        self.step_size = int(step_size)
        self.adc_full_scale_mv = float(adc_full_scale_mv)
        self.conductivity_uS_cm = float(conductivity_uS_cm)
        self.max_gap_fill = 20

        self.ref_idx = [0, 1, 2, 3, 6, 7]
        self.right_idx = [4, 5]

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._last_history_index = 0
        self._sample_buffer = deque(maxlen=max(self.window_size * 4, 400))
        self._samples_since_compute = 0
        self._last_seq = None
        self._last_pose = None
        self._seq_gap_count = 0
        self._seq_filled_count = 0
        self._seq_reset_count = 0

        self._history = deque(maxlen=240)
        self._trajectory = deque(maxlen=600)
        self._baseline_channels_mv = None
        self._baseline_feature_mv = None
        self._baseline_saved_at = None
        self._baseline_source = None

        self._capture_mode = None
        self._capture_windows = []
        self._pending_baseline_channels_mv = None
        self._pending_baseline_feature_mv = None
        self._compute_enabled = False

        self._state = self._empty_state()
        self.load_baseline()
        self._sync_baseline_state()

    def _sync_baseline_state(self) -> None:
        self._state["baseline_ready"] = self._baseline_feature_mv is not None
        self._state["baseline_feature_mv"] = self._baseline_feature_mv
        self._state["baseline_saved_at"] = self._baseline_saved_at
        self._state["baseline_source"] = self._baseline_source
        self._state["compute_enabled"] = self._compute_enabled
        self._state["pending_baseline_ready"] = self._pending_baseline_feature_mv is not None
        self._state["pending_baseline_feature_mv"] = self._pending_baseline_feature_mv
        if self._baseline_channels_mv is not None:
            self._state["baseline_channel_amp_mv"] = list(self._baseline_channels_mv)
        else:
            self._state["baseline_channel_amp_mv"] = None
        if self._pending_baseline_channels_mv is not None:
            self._state["pending_baseline_channel_amp_mv"] = list(self._pending_baseline_channels_mv)
        else:
            self._state["pending_baseline_channel_amp_mv"] = None

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
            "compute_enabled": self._compute_enabled,
            "pending_baseline_ready": self._pending_baseline_feature_mv is not None,
            "pending_baseline_feature_mv": self._pending_baseline_feature_mv,
            "capturing_null": False,
            "null_progress": 0.0,
            "null_windows_collected": 0,
            "null_windows_target": 0,
            "peak_freq_hz": None,
            "peak_amp_mv": None,
            "target_amp_mv": None,
            "raw_feature_mv": None,
            "delta_feature_mv": None,
            "channel_amp_mv": [None] * 8,
            "channel_delta_mv": [None] * 8,
            "history": [],
            "position": None,
            "trajectory": [],
            "trajectory_enabled": False,
            "last_seq": None,
            "seq_range": None,
            "seq_gap_count": 0,
            "seq_filled_count": 0,
            "seq_reset_count": 0,
            "window_received_samples": 0,
            "window_filled_samples": 0,
            "window_timestamp": None,
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
            self._pending_baseline_channels_mv = None
            self._pending_baseline_feature_mv = None
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
            self._last_pose = None
            self._seq_gap_count = 0
            self._seq_filled_count = 0
            self._seq_reset_count = 0
            self._history.clear()
            self._trajectory.clear()
            self._capture_mode = None
            self._capture_windows = []
            self._pending_baseline_channels_mv = None
            self._pending_baseline_feature_mv = None
            self._state["capturing_null"] = False
            self._state["null_progress"] = 0.0
            self._state["null_windows_collected"] = 0
            self._state["null_windows_target"] = 0
            self._state["pending_baseline_ready"] = False
            self._state["pending_baseline_feature_mv"] = None
            self._state["history"] = []
            self._state["position"] = None
            self._state["trajectory"] = []
            self._state["trajectory_enabled"] = False
            self._state["seq_gap_count"] = 0
            self._state["seq_filled_count"] = 0
            self._state["seq_reset_count"] = 0
            self._state["window_received_samples"] = 0
            self._state["window_filled_samples"] = 0

    def set_compute_enabled(self, enabled: bool) -> Dict[str, object]:
        with self._lock:
            self._compute_enabled = bool(enabled)
            self._state["compute_enabled"] = self._compute_enabled
            self._state["trajectory_enabled"] = bool(self._compute_enabled and self._state.get("position"))
            if not self._compute_enabled:
                self._state["ready"] = self._baseline_feature_mv is not None
                self._state["trajectory_enabled"] = False
        return self.get_state()

    def start_null_capture(self, seconds: float = 8.0) -> Dict[str, object]:
        with self._lock:
            self._capture_mode = {
                "captured_at": time.time(),
            }
            self._capture_windows = []
            self._state["capturing_null"] = True
            self._state["null_progress"] = 0.0
            self._state["null_windows_collected"] = 0
            self._state["null_windows_target"] = 0
            self._state["pending_baseline_ready"] = False
            self._state["pending_baseline_feature_mv"] = None
            self._pending_baseline_channels_mv = None
            self._pending_baseline_feature_mv = None
        return self.get_state()

    def stop_null_capture(self) -> Dict[str, object]:
        with self._lock:
            if self._capture_mode is None or not self._capture_windows:
                self._state["capturing_null"] = False
                self._state["null_progress"] = 0.0
                self._state["null_windows_collected"] = len(self._capture_windows)
            else:
                captured = len(self._capture_windows)
                baseline = [0.0] * 8
                for ch in range(8):
                    baseline[ch] = sum(window_amp[ch] for window_amp in self._capture_windows) / float(captured)
                feature = (
                    sum(baseline[i] for i in self.right_idx) / len(self.right_idx)
                    - sum(baseline[i] for i in self.ref_idx) / len(self.ref_idx)
                )
                self._pending_baseline_channels_mv = baseline
                self._pending_baseline_feature_mv = feature
                self._capture_mode = None
                self._capture_windows = []
                self._state["capturing_null"] = False
                self._state["null_progress"] = 1.0
                self._state["null_windows_collected"] = captured
                self._state["pending_baseline_ready"] = True
                self._state["pending_baseline_feature_mv"] = feature
                self._state["pending_baseline_channel_amp_mv"] = list(baseline)
        return self.get_state()

    def save_baseline(self) -> Dict[str, object]:
        with self._lock:
            if self._pending_baseline_feature_mv is None or self._pending_baseline_channels_mv is None:
                return self.get_state()
            self._baseline_channels_mv = list(self._pending_baseline_channels_mv)
            self._baseline_feature_mv = float(self._pending_baseline_feature_mv)
            self._baseline_saved_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            self._baseline_source = "captured"
            self._save_baseline()
            self._pending_baseline_channels_mv = None
            self._pending_baseline_feature_mv = None
            self._state["baseline_ready"] = True
            self._state["baseline_feature_mv"] = self._baseline_feature_mv
            self._state["baseline_saved_at"] = self._baseline_saved_at
            self._state["baseline_source"] = self._baseline_source
            self._state["pending_baseline_ready"] = False
            self._state["pending_baseline_feature_mv"] = None
            self._state["pending_baseline_channel_amp_mv"] = None
            self._sync_baseline_state()
        return self.get_state()

    def get_state(self) -> Dict[str, object]:
        with self._lock:
            state = dict(self._state)
            if self._baseline_channels_mv is not None:
                state["baseline_channel_amp_mv"] = list(self._baseline_channels_mv)
            else:
                state["baseline_channel_amp_mv"] = None
            state["history"] = list(self._history)
            state["trajectory"] = list(self._trajectory)
            state["trajectory_enabled"] = bool(self._compute_enabled and self._state.get("position"))
            return state

    def _append_history(self, snapshot: Dict[str, object]) -> None:
        entry = {
            "ts": time.time(),
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
        if match.group("hh") is not None:
            hh = int(match.group("hh"))
            mm = int(match.group("mm"))
            ss = int(match.group("ss"))
            ms = int(match.group("ms"))
            sample_ts = ((hh * 60 + mm) * 60 + ss) + ms / 1000.0
        else:
            sample_ts = time.time()
        seq = int(match.group("seq"))
        counts = [int(match.group(f"ch{i}")) for i in range(8)]
        pose = None
        if match.group("pos_valid") == "1":
            pose = {
                "id": int(match.group("pos_id")),
                "x": int(match.group("pos_x")),
                "y": int(match.group("pos_y")),
                "z": int(match.group("pos_z")),
                "valid": True,
                "pseq": int(match.group("pos_seq")),
            }
        return {
            "seq": seq,
            "ts": sample_ts,
            "counts": counts,
            "pose": pose,
            "filled": False,
            "drop": int(match.group("drop")),
            "overrun": int(match.group("overrun")),
        }

    def ingest_history(self, entries: Sequence[Dict[str, object]]) -> None:
        def append_sample(sample: Dict[str, object]) -> None:
            if sample.get("pose") is not None:
                self._last_pose = sample["pose"]
            self._sample_buffer.append(sample)
            self._samples_since_compute += 1
            should_compute = self._compute_enabled or self._capture_mode is not None
            if should_compute and len(self._sample_buffer) >= self.window_size and self._samples_since_compute >= self.step_size:
                self._samples_since_compute = 0
                self._compute_state()

        for entry in entries:
            raw = entry.get("raw", "")
            sample = self._parse_sample(raw)
            if sample is None:
                continue
            seq = sample["seq"]
            if self._last_seq is not None:
                gap = seq - self._last_seq
                if gap <= 0:
                    continue
                if gap > 1:
                    self._seq_gap_count += 1
                    missing_count = gap - 1
                    if missing_count <= self.max_gap_fill and self._sample_buffer:
                        prev = self._sample_buffer[-1]
                        prev_counts = prev["counts"]
                        prev_ts = float(prev.get("ts", sample["ts"]))
                        sample_ts = float(sample["ts"])
                        dt = (sample_ts - prev_ts) / float(gap) if sample_ts >= prev_ts else 1.0 / self.sample_rate_hz
                        for missing in range(1, gap):
                            ratio = missing / float(gap)
                            filled_counts = [
                                int(round(float(prev_counts[ch]) + (float(sample["counts"][ch]) - float(prev_counts[ch])) * ratio))
                                for ch in range(8)
                            ]
                            filled_sample = {
                                "seq": self._last_seq + missing,
                                "ts": prev_ts + dt * missing,
                                "counts": filled_counts,
                                "pose": self._last_pose,
                                "filled": True,
                                "drop": sample.get("drop"),
                                "overrun": sample.get("overrun"),
                            }
                            self._seq_filled_count += 1
                            append_sample(filled_sample)
                    else:
                        self._sample_buffer.clear()
                        self._samples_since_compute = 0
                        self._seq_reset_count += 1
            self._last_seq = seq
            append_sample(sample)

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
        filled_samples = sum(1 for row in window if row.get("filled"))
        received_samples = len(window) - filled_samples

        with self._lock:
            pose = window[-1].get("pose") or self._last_pose
            if self._capture_mode is not None:
                self._capture_windows.append(feats["channel_amp_mv"])
                captured = len(self._capture_windows)
                self._state["capturing_null"] = True
                self._state["null_windows_collected"] = captured
                self._state["null_windows_target"] = 0
                self._state["null_progress"] = 0.0
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
                "channel_amp_mv": [round(v, 3) for v in feats["channel_amp_mv"]],
                "channel_delta_mv": [None if v is None else round(v, 3) for v in feats["channel_delta_mv"]],
                "window_timestamp": window[-1]["ts"],
                "last_seq": window[-1]["seq"],
                "seq_range": [window[0]["seq"], window[-1]["seq"]],
                "seq_gap_count": self._seq_gap_count,
                "seq_filled_count": self._seq_filled_count,
                "seq_reset_count": self._seq_reset_count,
                "window_received_samples": received_samples,
                "window_filled_samples": filled_samples,
            })
            if pose is not None:
                position = {
                    "id": pose.get("id"),
                    "x": pose.get("x"),
                    "y": pose.get("y"),
                    "z": pose.get("z"),
                    "pseq": pose.get("pseq"),
                    "valid": bool(pose.get("valid")),
                    "ts": window[-1]["ts"],
                    "seq": window[-1]["seq"],
                }
                self._state["position"] = position
                if self._compute_enabled and self._capture_mode is None and position["valid"]:
                    if not self._trajectory or self._trajectory[-1].get("pseq") != position["pseq"]:
                        self._trajectory.append(position)
                self._state["trajectory"] = list(self._trajectory)
                self._state["trajectory_enabled"] = bool(self._compute_enabled and position["valid"])
            else:
                self._state["trajectory_enabled"] = False
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
