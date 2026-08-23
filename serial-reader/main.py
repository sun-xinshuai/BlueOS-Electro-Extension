#!/usr/bin/env python3
"""BlueOS serial reader focused on active electro FFT and trajectory display."""

from __future__ import annotations

import json
from pathlib import Path

from flask import Flask, request as flask_request

from electro_analysis import ElectroAnalyzer
from serial_driver import SerialDriver


app = Flask(__name__, static_url_path="/static", static_folder="static")


class API:
    def __init__(self, driver: SerialDriver, analyzer: ElectroAnalyzer):
        self.driver = driver
        self.analyzer = analyzer

    def get_status(self):
        return self.driver.get_status()

    def get_history_since(self, since, limit=2000):
        return self.driver.get_history_since(since, limit)

    def export_history(self, limit=30000):
        return self.driver.export_history(limit)

    def set_enabled(self, enabled):
        if enabled in ["true", "false"]:
            return self.driver.set_enabled(enabled == "true")
        return False

    def set_port(self, port):
        return self.driver.set_port(port)

    def set_baud(self, baud):
        try:
            return self.driver.set_baud(int(baud))
        except Exception:
            return False

    def clear_history(self):
        self.analyzer.reset_stream()
        self.driver.clear_history()
        return True

    def list_ports(self):
        return self.driver.list_ports()

    def get_electro_state(self):
        return self.analyzer.get_state()

    def capture_null(self, seconds=8.0):
        return self.analyzer.start_null_capture(seconds)

    def stop_null_capture(self):
        return self.analyzer.stop_null_capture()

    def save_baseline(self):
        return self.analyzer.save_baseline()

    def set_compute_enabled(self, enabled):
        return self.analyzer.set_compute_enabled(enabled)

    def clear_baseline(self):
        self.analyzer.clear_baseline()
        return True

    def load_baseline(self):
        return self.analyzer.load_baseline()


driver = SerialDriver()
baseline_path = Path(__file__).with_name("electro_baseline.json")
analyzer = ElectroAnalyzer(
    driver=driver,
    baseline_path=baseline_path,
    sample_rate_hz=100.0,
    target_hz=20.0,
    window_size=100,
    step_size=10,
    adc_full_scale_mv=10000.0,
    conductivity_uS_cm=800.0,
)
api = API(driver, analyzer)


@app.route("/register_service")
def register_service():
    return app.send_static_file("service.json")


@app.route("/")
def root():
    return app.send_static_file("index.html")


@app.route("/health")
def health():
    return json.dumps({"ok": True, "service": "serial-reader", "mode": "fft-trajectory"})


@app.route("/get_status")
def get_status():
    return json.dumps(api.get_status())


@app.route("/get_history_since/<int:since>")
def get_history_since(since):
    return json.dumps(api.get_history_since(since))


@app.route("/export_history")
def export_history():
    try:
        limit = int(flask_request.args.get("limit", 30000))
    except Exception:
        limit = 30000
    return json.dumps(api.export_history(limit))


@app.route("/enable/<enable>")
def set_enabled(enable):
    return str(api.set_enabled(enable))


@app.route("/set_port/<path:port>")
def set_port(port):
    return str(api.set_port(port))


@app.route("/set_baud/<baud>")
def set_baud(baud):
    return str(api.set_baud(baud))


@app.route("/clear_history")
def clear_history():
    return str(api.clear_history())


@app.route("/list_ports")
def list_ports():
    return json.dumps(api.list_ports())


@app.route("/get_electro_state")
def get_electro_state():
    return json.dumps(api.get_electro_state())


@app.route("/capture_null")
def capture_null():
    try:
        seconds = float(flask_request.args.get("seconds", 8.0))
    except Exception:
        seconds = 8.0
    return json.dumps(api.capture_null(seconds))


@app.route("/stop_null_capture")
def stop_null_capture():
    return json.dumps(api.stop_null_capture())


@app.route("/save_baseline")
def save_baseline():
    return json.dumps(api.save_baseline())


@app.route("/set_compute/<enable>")
def set_compute(enable):
    if enable in ["true", "false"]:
        return json.dumps(api.set_compute_enabled(enable == "true"))
    return json.dumps(False)


@app.route("/clear_baseline")
def clear_baseline():
    return json.dumps(api.clear_baseline())


@app.route("/load_baseline")
def load_baseline():
    return json.dumps(api.load_baseline())


if __name__ == "__main__":
    driver.start()
    analyzer.start()
    app.run(host="0.0.0.0", port=9001, threaded=True)
