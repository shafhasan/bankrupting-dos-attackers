from __future__ import annotations

import argparse
import json
import math
import socketserver
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common import CsvAppender, json_dumps_line


@dataclass
class PricingState:
    """Single global pricing state shared by every arriving job."""

    iteration_id: int
    iteration_start: float
    current_length: float
    serviced_in_iteration: int = 0


class LinearEngine:
    """Atomic pricing state for LINEAR and LINEAR-POWER.

    The lock is held only while one request is priced and its server state is
    updated. It is never held while a client waits before a retry. The retry
    scheduler lives in trace_controller.py.
    """

    def __init__(self, output_csv: Path, algorithm: str, attempt_output_csv: Path | None = None):
        if algorithm not in {"linear", "linear-power"}:
            raise ValueError("algorithm must be 'linear' or 'linear-power'")
        self.algorithm = algorithm
        self.lock = threading.Lock()
        self.latest_iteration_length: float | None = None
        self.latest_estimator_version = 0
        self.latest_estimator_source = "unset"
        self.state: PricingState | None = None
        self.pending_attempts: dict[str, dict[str, float | int]] = {}

        self.logger = CsvAppender(
            output_csv,
            [
                "sequence_id",
                "flow_uid",
                "generated_trace_time",
                "trace_time",
                "timestamp",
                "source_ip",
                "source_port",
                "destination_ip",
                "destination_port",
                "protocol",
                "transport",
                "algorithm",
                "logical_server_key",
                "iteration_id",
                "iteration_start",
                "iteration_length",
                "estimator_version",
                "estimator_source",
                "serviced_before",
                "price",
                "attached_fee",
                "accepted_fee",
                "submitted_fee_sum",
                "total_fee_paid",
                "attempt_count",
                "rejection_count",
                "bounced_attempts",
                "overpayment",
                "service_cost",
                "total_fwd_packets",
                "total_backward_packets",
                "total_fwd_bytes",
                "total_backward_bytes",
                "flow_duration",
            ],
        )
        self.attempt_logger = (
            CsvAppender(
                attempt_output_csv,
                [
                    "sequence_id",
                    "flow_uid",
                    "generated_trace_time",
                    "trace_time",
                    "timestamp",
                    "source_ip",
                    "destination_ip",
                    "destination_port",
                    "protocol",
                    "transport",
                    "algorithm",
                    "logical_server_key",
                    "iteration_id",
                    "iteration_start",
                    "iteration_length",
                    "serviced_before",
                    "attempt_number",
                    "attached_fee",
                    "required_price",
                    "serviced",
                    "accepted_fee",
                    "submitted_fee_sum",
                    "cumulative_submitted_fee",
                    "rejection_count",
                ],
            )
            if attempt_output_csv is not None
            else None
        )

    @staticmethod
    def _linear_power_price(serviced_before: int) -> int:
        # 2^floor(log2(s + 1)), where s is the number already serviced.
        value = serviced_before + 1
        return 1 << (value.bit_length() - 1)

    def _required_price(self, serviced_before: int) -> int:
        if self.algorithm == "linear":
            return serviced_before + 1
        return self._linear_power_price(serviced_before)

    def _state_for(self, trace_time: float) -> PricingState:
        if self.latest_iteration_length is None:
            raise RuntimeError("Pricing server has not been initialized with an iteration length")
        if self.state is None:
            self.state = PricingState(
                iteration_id=0,
                iteration_start=trace_time,
                current_length=self.latest_iteration_length,
            )

        state = self.state

        # Preserve exact mathematical boundaries, including empty iterations.
        # There is one global iteration clock and one global serviced-job
        # counter shared by all source/destination IPs and both transports.
        # Retry attempts carry their own logical arrival time, so a retry may
        # legitimately arrive in a later iteration.
        while trace_time - state.iteration_start >= state.current_length:
            state.iteration_start += state.current_length
            state.iteration_id += 1
            state.serviced_in_iteration = 0
            state.current_length = self.latest_iteration_length
        return state

    def reset(self, iteration_length: float, version: int, source: str) -> dict[str, Any]:
        if not (math.isfinite(iteration_length) and iteration_length > 0):
            raise ValueError("iteration_length must be positive and finite")
        with self.lock:
            self.latest_iteration_length = iteration_length
            self.latest_estimator_version = version
            self.latest_estimator_source = source
            self.state = None
            self.pending_attempts.clear()
            return {
                "ok": True,
                "algorithm": self.algorithm,
                "iteration_length": iteration_length,
            }

    def set_iteration_length(self, iteration_length: float, version: int, source: str) -> dict[str, Any]:
        if not (math.isfinite(iteration_length) and iteration_length > 0):
            raise ValueError("iteration_length must be positive and finite")
        with self.lock:
            self.latest_iteration_length = iteration_length
            self.latest_estimator_version = version
            self.latest_estimator_source = source
            return {
                "ok": True,
                "algorithm": self.algorithm,
                "iteration_length": iteration_length,
                "version": version,
                "application": "next global iteration boundary",
            }

    def quote(self, trace_time: float) -> dict[str, Any]:
        with self.lock:
            state = self._state_for(trace_time)
            price = self._required_price(state.serviced_in_iteration)
            return {
                "ok": True,
                "algorithm": self.algorithm,
                "logical_server_key": "GLOBAL",
                "iteration_id": state.iteration_id,
                "iteration_length": state.current_length,
                "serviced_before": state.serviced_in_iteration,
                "price": price,
            }

    def abandon_flow(self, flow_uid: str) -> dict[str, Any]:
        """Discard retry bookkeeping after timeout or client-side failure."""
        with self.lock:
            removed = self.pending_attempts.pop(str(flow_uid), None)
            return {"ok": True, "flow_uid": str(flow_uid), "removed": removed is not None}

    def process_flow(self, flow: dict[str, Any], transport: str) -> dict[str, Any]:
        with self.lock:
            trace_time = float(flow["trace_time"])
            destination_ip = str(flow["destination_ip"])
            # Destination IP remains trace metadata only. All jobs compete
            # against one global pricing state, matching the paper's
            # single-server model.
            server_key = "GLOBAL"
            state = self._state_for(trace_time)
            serviced_before = state.serviced_in_iteration
            required_price = self._required_price(serviced_before)
            flow_uid = str(flow["flow_uid"])

            if self.algorithm == "linear":
                # Zero-latency LINEAR baseline: the client is modeled as knowing
                # the exact current server price when it submits the job.  The
                # request therefore arrives with exactly the required fee and
                # is serviced in one attempt; there is no bounce/retry path.
                attached_fee = float(required_price)
                submitted_fee_sum = attached_fee
                attempt_count = 1
                rejection_count = 0
                serviced = True
                accepted_fee = attached_fee
            else:
                # LINEAR-POWER keeps the asynchronous fee-validation behavior.
                # A good flow may arrive with a stale fee, be bounced, update
                # its fee, and retry later while other jobs continue to run.
                attached_fee = float(flow.get("fee", 0.0))
                if not (math.isfinite(attached_fee) and attached_fee >= 0):
                    raise ValueError("attached fee must be finite and non-negative")
                pending = self.pending_attempts.setdefault(
                    flow_uid,
                    {
                        "submitted_fee_sum": 0.0,
                        "attempt_count": 0,
                        "rejection_count": 0,
                    },
                )
                pending["submitted_fee_sum"] = float(pending["submitted_fee_sum"]) + attached_fee
                pending["attempt_count"] = int(pending["attempt_count"]) + 1
                submitted_fee_sum = float(pending["submitted_fee_sum"])
                attempt_count = int(pending["attempt_count"])
                serviced = attached_fee >= required_price
                if not serviced:
                    pending["rejection_count"] = int(pending["rejection_count"]) + 1
                rejection_count = int(pending["rejection_count"])
                accepted_fee = attached_fee if serviced else 0.0

            if self.attempt_logger is not None:
                self.attempt_logger.write(
                    {
                        **flow,
                        "transport": transport,
                        "algorithm": self.algorithm,
                        "logical_server_key": server_key,
                        "iteration_id": state.iteration_id,
                        "iteration_start": state.iteration_start,
                        "iteration_length": state.current_length,
                        "serviced_before": serviced_before,
                        "attempt_number": attempt_count,
                        "attached_fee": attached_fee,
                        "required_price": required_price,
                        "serviced": serviced,
                        "accepted_fee": accepted_fee,
                        "submitted_fee_sum": submitted_fee_sum,
                        "cumulative_submitted_fee": submitted_fee_sum,
                        "rejection_count": rejection_count,
                    }
                )

            if not serviced:
                return {
                    "ok": True,
                    "serviced": False,
                    "flow_uid": flow_uid,
                    "sequence_id": flow["sequence_id"],
                    "algorithm": self.algorithm,
                    "price": required_price,
                    "attached_fee": attached_fee,
                    "accepted_fee": 0.0,
                    "submitted_fee_sum": submitted_fee_sum,
                    "total_fee_paid": submitted_fee_sum,
                    "attempt_count": attempt_count,
                    "rejection_count": rejection_count,
                    "bounced_attempts": rejection_count,
                    "iteration_id": state.iteration_id,
                    "iteration_length": state.current_length,
                    "logical_server_key": server_key,
                }

            state.serviced_in_iteration += 1
            overpayment = max(0.0, attached_fee - required_price)
            row = {
                **flow,
                "transport": transport,
                "algorithm": self.algorithm,
                "logical_server_key": server_key,
                "iteration_id": state.iteration_id,
                "iteration_start": state.iteration_start,
                "iteration_length": state.current_length,
                "estimator_version": self.latest_estimator_version,
                "estimator_source": self.latest_estimator_source,
                "serviced_before": serviced_before,
                "price": required_price,
                "attached_fee": attached_fee,
                "accepted_fee": accepted_fee,
                "submitted_fee_sum": submitted_fee_sum,
                # Kept for backward compatibility. The evaluator applies the
                # requested label-specific charging rule.
                "total_fee_paid": submitted_fee_sum,
                "attempt_count": attempt_count,
                "rejection_count": rejection_count,
                "bounced_attempts": rejection_count,
                "overpayment": overpayment,
                "service_cost": 1,
            }
            self.logger.write(row)
            self.pending_attempts.pop(flow_uid, None)
            return {
                "ok": True,
                "serviced": True,
                "flow_uid": flow_uid,
                "sequence_id": flow["sequence_id"],
                "algorithm": self.algorithm,
                "price": required_price,
                "attached_fee": attached_fee,
                "accepted_fee": accepted_fee,
                "submitted_fee_sum": submitted_fee_sum,
                "total_fee_paid": submitted_fee_sum,
                "attempt_count": attempt_count,
                "rejection_count": rejection_count,
                "bounced_attempts": rejection_count,
                "overpayment": overpayment,
                "iteration_id": state.iteration_id,
                "iteration_length": state.current_length,
                "logical_server_key": server_key,
            }

    def flush(self) -> None:
        with self.lock:
            self.logger.flush()
            if self.attempt_logger is not None:
                self.attempt_logger.flush()

    def close(self) -> None:
        with self.lock:
            self.logger.close()
            if self.attempt_logger is not None:
                self.attempt_logger.close()


ENGINE: LinearEngine


class TcpFlowHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        for raw in self.rfile:
            if not raw.strip():
                continue
            try:
                message = json.loads(raw.decode("utf-8"))
                if message.get("message_type") != "flow":
                    raise ValueError("TCP flow listener accepts only flow messages")
                response = ENGINE.process_flow(message, transport="TCP")
            except Exception as exc:
                response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self.wfile.write(json_dumps_line(response))
            self.wfile.flush()


class ControlHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        for raw in self.rfile:
            if not raw.strip():
                continue
            try:
                message = json.loads(raw.decode("utf-8"))
                command = message.get("command")
                if command == "reset":
                    response = ENGINE.reset(
                        float(message["iteration_length"]),
                        int(message.get("version", 0)),
                        str(message.get("source", "initial_calibration")),
                    )
                elif command == "set_iteration_length":
                    response = ENGINE.set_iteration_length(
                        float(message["iteration_length"]),
                        int(message.get("version", 0)),
                        str(message.get("source", "sliding_window")),
                    )
                elif command == "quote":
                    response = ENGINE.quote(float(message["trace_time"]))
                elif command == "abandon_flow":
                    response = ENGINE.abandon_flow(str(message["flow_uid"]))
                elif command == "flush":
                    ENGINE.flush()
                    response = {"ok": True}
                elif command == "status":
                    with ENGINE.lock:
                        response = {
                            "ok": True,
                            "algorithm": ENGINE.algorithm,
                            "iteration_length": ENGINE.latest_iteration_length,
                            "version": ENGINE.latest_estimator_version,
                            "source": ENGINE.latest_estimator_source,
                            "pricing_states": 1 if ENGINE.state is not None else 0,
                            "pricing_scope": "global",
                            "pending_jobs": len(ENGINE.pending_attempts),
                        }
                elif command == "shutdown":
                    response = {"ok": True}
                    self.wfile.write(json_dumps_line(response))
                    self.wfile.flush()
                    threading.Thread(target=shutdown_all, daemon=True).start()
                    return
                else:
                    raise ValueError(f"Unknown command: {command}")
            except Exception as exc:
                response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self.wfile.write(json_dumps_line(response))
            self.wfile.flush()


class ThreadingTcpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class ThreadingUdpServer(socketserver.ThreadingUDPServer):
    allow_reuse_address = True
    daemon_threads = True


class UdpFlowHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        data, sock = self.request
        try:
            message = json.loads(data.decode("utf-8"))
            if message.get("message_type") != "flow":
                raise ValueError("UDP flow listener accepts only flow messages")
            response = ENGINE.process_flow(message, transport="UDP")
        except Exception as exc:
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        sock.sendto(json.dumps(response, separators=(",", ":")).encode("utf-8"), self.client_address)


SERVERS: list[socketserver.BaseServer] = []


def shutdown_all() -> None:
    for server in SERVERS:
        server.shutdown()
    ENGINE.close()


def main() -> None:
    global ENGINE, SERVERS
    parser = argparse.ArgumentParser(description="Trace-driven LINEAR/LINEAR-POWER server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--udp-port", type=int, default=19017)
    parser.add_argument("--tcp-port", type=int, default=19006)
    parser.add_argument("--control-port", type=int, default=19005)
    parser.add_argument("--algorithm", choices=["linear", "linear-power"], default="linear")
    parser.add_argument("--output", required=True)
    parser.add_argument("--attempt-output")
    args = parser.parse_args()

    ENGINE = LinearEngine(
        Path(args.output),
        args.algorithm,
        Path(args.attempt_output) if args.attempt_output else None,
    )
    udp_server = ThreadingUdpServer((args.host, args.udp_port), UdpFlowHandler)
    tcp_server = ThreadingTcpServer((args.host, args.tcp_port), TcpFlowHandler)
    control_server = ThreadingTcpServer((args.host, args.control_port), ControlHandler)
    SERVERS = [udp_server, tcp_server, control_server]

    threads = []
    for server, name in [
        (udp_server, "UDP"),
        (tcp_server, "TCP"),
        (control_server, "control"),
    ]:
        thread = threading.Thread(target=server.serve_forever, name=name, daemon=True)
        thread.start()
        threads.append(thread)
    print(
        f"{args.algorithm.upper()} server: UDP {args.host}:{args.udp_port}, "
        f"TCP {args.host}:{args.tcp_port}, control {args.host}:{args.control_port}",
        flush=True,
    )
    try:
        for thread in threads:
            thread.join()
    finally:
        ENGINE.close()


if __name__ == "__main__":
    main()
