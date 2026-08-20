from __future__ import annotations

import argparse
import math
import socketserver
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.stats import weibull_min

from common import BENIGN_LABEL, json_dumps_line


@dataclass
class EstimatorState:
    mode: str = "fixed"
    window_size: int = 200
    min_samples: int = 30
    refit_every: int = 10
    min_length: float = 1e-6
    max_length: float = 3600.0
    phase: str = "idle"
    last_good_time: float | None = None
    good_iats: deque[float] = field(default_factory=lambda: deque(maxlen=200))
    calibration_iats: list[float] = field(default_factory=list)
    good_observations: int = 0
    new_good_iats_since_fit: int = 0
    estimate_version: int = 0
    latest_estimate: dict[str, Any] | None = None
    frozen_estimate: dict[str, Any] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def reset(self, message: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.mode = str(message.get("mode", "fixed"))
            if self.mode not in {"fixed", "sliding"}:
                raise ValueError("mode must be 'fixed' or 'sliding'")
            self.window_size = int(message.get("window_size", 200))
            self.min_samples = int(message.get("min_samples", 30))
            self.refit_every = int(message.get("refit_every", 10))
            self.min_length = float(message.get("min_length", 1e-6))
            self.max_length = float(message.get("max_length", 3600.0))
            if self.window_size < self.min_samples:
                raise ValueError("window_size must be at least min_samples")
            self.phase = "calibration"
            self.last_good_time = None
            self.good_iats = deque(maxlen=self.window_size)
            self.calibration_iats = []
            self.good_observations = 0
            self.new_good_iats_since_fit = 0
            self.estimate_version = 0
            self.latest_estimate = None
            self.frozen_estimate = None
            return self.status_unlocked()

    def observe(self, message: dict[str, Any]) -> dict[str, Any]:
        trace_time = float(message["trace_time"])
        label = str(message["label"]).strip()
        with self.lock:
            updated = False
            if label == BENIGN_LABEL:
                self.good_observations += 1
                if self.last_good_time is not None:
                    iat = trace_time - self.last_good_time
                    if iat > 0 and math.isfinite(iat):
                        self.good_iats.append(iat)
                        if self.phase == "calibration":
                            self.calibration_iats.append(iat)
                        self.new_good_iats_since_fit += 1
                self.last_good_time = trace_time

                if (
                    self.phase == "evaluation"
                    and self.mode == "sliding"
                    and len(self.good_iats) >= self.min_samples
                    and self.new_good_iats_since_fit >= self.refit_every
                ):
                    self.latest_estimate = self._fit(list(self.good_iats), source="sliding_window")
                    self.new_good_iats_since_fit = 0
                    updated = True

            return {
                "ok": True,
                "updated": updated,
                "phase": self.phase,
                "mode": self.mode,
                "good_observations": self.good_observations,
                "window_samples": len(self.good_iats),
                "estimate": self.latest_estimate,
            }

    def freeze(self) -> dict[str, Any]:
        with self.lock:
            if len(self.calibration_iats) < self.min_samples:
                raise ValueError(
                    f"Need at least {self.min_samples} positive calibration inter-arrival samples; "
                    f"received {len(self.calibration_iats)}"
                )
            estimate = self._fit(self.calibration_iats, source="initial_calibration")
            self.frozen_estimate = dict(estimate)
            self.latest_estimate = dict(estimate)
            return {"ok": True, "estimate": estimate}

    def start_evaluation(self) -> dict[str, Any]:
        with self.lock:
            if self.frozen_estimate is None:
                raise ValueError("freeze must be called before start_evaluation")
            self.phase = "evaluation"
            # The calibration rows are also replayed in the measured workload,
            # but trace_controller deliberately does not feed those same benign
            # rows to the estimator twice.  Keep the calibration IATs as the
            # initial sliding window and retain the timestamp of the final
            # calibration good flow.  This lets the first benign flow *after*
            # calibration form the correct next IAT.
            if self.mode == "sliding":
                self.good_iats = deque(self.calibration_iats[-self.window_size :], maxlen=self.window_size)
            else:
                self.good_iats = deque(maxlen=self.window_size)
                self.last_good_time = None
            self.new_good_iats_since_fit = 0
            return self.status_unlocked()

    def _fit(self, samples: list[float], source: str) -> dict[str, Any]:
        values = np.asarray(samples, dtype=float)
        values = values[np.isfinite(values) & (values > 0)]
        if values.size < self.min_samples:
            raise ValueError("Not enough valid samples for Weibull fitting")
        shape, _, scale = weibull_min.fit(values, floc=0)
        if not (math.isfinite(shape) and shape > 0 and math.isfinite(scale) and scale > 0):
            raise ValueError("Weibull fit produced invalid parameters")
        mean_iat = float(scale * math.gamma(1.0 + 1.0 / shape))
        mean_iat = min(max(mean_iat, self.min_length), self.max_length)
        self.estimate_version += 1
        return {
            "version": self.estimate_version,
            "source": source,
            "shape": float(shape),
            "scale": float(scale),
            "mean_iat": mean_iat,
            "good_rate": float(1.0 / mean_iat),
            "iteration_length": mean_iat,
            "sample_count": int(values.size),
        }

    def status_unlocked(self) -> dict[str, Any]:
        return {
            "ok": True,
            "phase": self.phase,
            "mode": self.mode,
            "window_size": self.window_size,
            "min_samples": self.min_samples,
            "refit_every": self.refit_every,
            "good_observations": self.good_observations,
            "window_samples": len(self.good_iats),
            "calibration_samples": len(self.calibration_iats),
            "estimate": self.latest_estimate,
        }

    def status(self) -> dict[str, Any]:
        with self.lock:
            return self.status_unlocked()


STATE = EstimatorState()


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        for raw in self.rfile:
            if not raw.strip():
                continue
            try:
                import json

                message = json.loads(raw.decode("utf-8"))
                command = message.get("command")
                if command == "reset":
                    response = STATE.reset(message)
                elif command == "observe":
                    response = STATE.observe(message)
                elif command == "freeze":
                    response = STATE.freeze()
                elif command == "start_evaluation":
                    response = STATE.start_evaluation()
                elif command == "status":
                    response = STATE.status()
                elif command == "shutdown":
                    response = {"ok": True}
                    self.wfile.write(json_dumps_line(response))
                    self.wfile.flush()
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                else:
                    raise ValueError(f"Unknown command: {command}")
            except Exception as exc:
                response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self.wfile.write(json_dumps_line(response))
            self.wfile.flush()


class ThreadingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser(description="Label-aware online Weibull estimator service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19100)
    args = parser.parse_args()
    with ThreadingServer((args.host, args.port), Handler) as server:
        print(f"Estimator listening on {args.host}:{args.port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
