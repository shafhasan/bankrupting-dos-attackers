from __future__ import annotations

import argparse
import heapq
import json
import math
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from common import (
    BENIGN_LABEL,
    PROTOCOL_TCP,
    PROTOCOL_UDP,
    CsvAppender,
    JsonLineClient,
    TracePacer,
    json_dumps_line,
    recv_json_line,
    strip_columns,
)


REQUIRED_COLUMNS = {
    "Source IP",
    "Source Port",
    "Destination IP",
    "Destination Port",
    "Protocol",
    "Timestamp",
    "Label",
}


@dataclass
class TraceClient:
    source_ip: str
    server_host: str
    udp_port: int
    tcp_port: int
    timeout: float = 10.0
    udp_socket: socket.socket | None = None
    tcp_socket: socket.socket | None = None
    tcp_buffer: bytearray | None = None

    def send_once(self, flow: dict[str, Any]) -> dict[str, Any]:
        """Send every dataset flow to the pricing server.

        Protocol 17 is replayed over the UDP data socket and protocol 6 over the
        TCP data socket.  Any other original IP protocol is still included in
        the experiment and retains its original Protocol value as metadata; it
        is serialized over the TCP data socket as a replay transport.  The
        pricing algorithm is flow-level and does not inspect the transport used
        to carry the JSON replay message.
        """
        protocol = int(flow["protocol"])
        if protocol == PROTOCOL_UDP:
            return self._send_udp(flow)
        return self._send_tcp(flow)

    def _send_udp(self, flow: dict[str, Any]) -> dict[str, Any]:
        if self.udp_socket is None:
            self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp_socket.settimeout(self.timeout)
        payload = json.dumps(flow, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.udp_socket.sendto(payload, (self.server_host, self.udp_port))
        response, _ = self.udp_socket.recvfrom(65535)
        return json.loads(response.decode("utf-8"))

    def _connect_tcp(self) -> None:
        self.close_tcp()
        self.tcp_socket = socket.create_connection((self.server_host, self.tcp_port), timeout=self.timeout)
        self.tcp_socket.settimeout(self.timeout)
        self.tcp_buffer = bytearray()

    def _send_tcp(self, flow: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(2):
            try:
                if self.tcp_socket is None:
                    self._connect_tcp()
                assert self.tcp_socket is not None
                self.tcp_socket.sendall(json_dumps_line(flow))
                response, self.tcp_buffer = recv_json_line(self.tcp_socket, self.tcp_buffer)
                return response
            except (OSError, ConnectionError):
                self._connect_tcp()
                if attempt == 1:
                    raise
        raise RuntimeError("unreachable")

    def close_tcp(self) -> None:
        if self.tcp_socket is not None:
            try:
                self.tcp_socket.close()
            finally:
                self.tcp_socket = None
                self.tcp_buffer = None

    def close(self) -> None:
        if self.udp_socket is not None:
            self.udp_socket.close()
            self.udp_socket = None
        self.close_tcp()


@dataclass
class FlowRuntime:
    sequence_id: int
    row: pd.Series
    base_message: dict[str, Any]
    is_good: bool
    generated_trace_time: float
    deadline_trace_time: float | None
    current_fee: float = 1.0
    attempt_count: int = 0
    rejection_count: int = 0
    submitted_fee_sum: float = 0.0
    last_required_price: float | None = None
    last_attached_fee: float | None = None
    first_wall_time: float | None = None
    estimator_update_version: str | int = ""
    terminal: bool = False
    result: dict[str, Any] = field(default_factory=dict)


def benign_mask(df: pd.DataFrame) -> pd.Series:
    """Return a strict boolean mask for BENIGN rows."""
    return (
        df["Label"]
        .astype("string")
        .str.replace("\ufeff", "", regex=False)
        .str.strip()
        .str.casefold()
        .eq("benign")
        .fillna(False)
        .astype(bool)
    )


def load_trace(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path, low_memory=False)
    df.columns = strip_columns(df.columns)
    missing = REQUIRED_COLUMNS.difference(df.columns)
    if missing:
        raise ValueError(f"CSV is missing required columns: {sorted(missing)}")

    df = df.copy()
    for column in ("Source IP", "Destination IP", "Timestamp", "Label"):
        df[column] = (
            df[column]
            .astype("string")
            .str.replace("\ufeff", "", regex=False)
            .str.strip()
        )

    # Rows without a usable label cannot be assigned to either the good or
    # adversary cost, so exclude them rather than silently treating them as bad.
    missing_label_mask = df["Label"].isna() | df["Label"].eq("").fillna(False)
    missing_label_rows = int(missing_label_mask.sum())
    if missing_label_rows > 0:
        print(
            json.dumps(
                {"rows_with_missing_labels_excluded": missing_label_rows},
                indent=2,
            )
        )
        df = df.loc[~missing_label_mask].copy()

    df["is_good"] = benign_mask(df)
    df.loc[df["is_good"], "Label"] = BENIGN_LABEL

    try:
        df["parsed_timestamp"] = pd.to_datetime(df["Timestamp"], errors="coerce", format="mixed")
    except (TypeError, ValueError):
        df["parsed_timestamp"] = pd.to_datetime(df["Timestamp"], errors="coerce")
    df = df[df["parsed_timestamp"].notna()].copy()

    df["Protocol"] = pd.to_numeric(df["Protocol"], errors="coerce").fillna(-1).astype(int)
    df["Source Port"] = pd.to_numeric(df["Source Port"], errors="coerce").fillna(0).astype(int)
    df["Destination Port"] = pd.to_numeric(df["Destination Port"], errors="coerce").fillna(0).astype(int)
    if "Unnamed: 0" in df.columns and df["Unnamed: 0"].is_unique:
        df["flow_uid"] = df["Unnamed: 0"].map(lambda x: f"row-{x}")
    else:
        df["flow_uid"] = [f"row-{i}" for i in df.index]
    return df.sort_values(["parsed_timestamp", "flow_uid"], kind="stable").reset_index(drop=True)


def safe_number(row: pd.Series, column: str, default: float = 0.0) -> float:
    value = row.get(column, default)
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def flow_message(row: pd.Series, sequence_id: int) -> dict[str, Any]:
    ts = row["parsed_timestamp"]
    trace_time = float(ts.timestamp())
    return {
        "message_type": "flow",
        "sequence_id": sequence_id,
        "flow_uid": str(row["flow_uid"]),
        "generated_trace_time": trace_time,
        "trace_time": trace_time,
        "timestamp": ts.isoformat(),
        "source_ip": str(row["Source IP"]),
        "source_port": int(row["Source Port"]),
        "destination_ip": str(row["Destination IP"]),
        "destination_port": int(row["Destination Port"]),
        "protocol": int(row["Protocol"]),
        "flow_duration": safe_number(row, "Flow Duration"),
        "total_fwd_packets": safe_number(row, "Total Fwd Packets"),
        "total_backward_packets": safe_number(row, "Total Backward Packets"),
        "total_fwd_bytes": safe_number(row, "Total Length of Fwd Packets"),
        "total_backward_bytes": safe_number(row, "Total Length of Bwd Packets"),
    }


def attempt_message(runtime: FlowRuntime, event_time: float, fee: float | None) -> dict[str, Any]:
    message = dict(runtime.base_message)
    message["trace_time"] = float(event_time)
    message["timestamp"] = datetime.fromtimestamp(event_time).isoformat()
    message["generated_trace_time"] = runtime.generated_trace_time
    message["attempt_number"] = runtime.attempt_count + 1
    if fee is not None:
        message["fee"] = float(fee)
    return message


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay CSV flows as logical dataset clients")
    parser.add_argument("--csv", required=True)
    parser.add_argument("--mode", choices=["fixed", "sliding"], required=True)
    parser.add_argument("--algorithm", choices=["linear", "linear-power"], default="linear")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--server-host", default="127.0.0.1")
    parser.add_argument("--udp-port", type=int, default=19017)
    parser.add_argument("--tcp-port", type=int, default=19006)
    parser.add_argument("--control-port", type=int, default=19005)
    parser.add_argument("--estimator-host", default="127.0.0.1")
    parser.add_argument("--estimator-port", type=int, default=19100)
    # Deprecated compatibility options. Client sampling is no longer performed:
    # every source IP represented by the evaluation trace is included.
    parser.add_argument(
        "--benign-client-count",
        type=int,
        default=None,
        help="Deprecated and ignored; all clients are included",
    )
    parser.add_argument(
        "--min-benign-flows-per-client",
        type=int,
        default=None,
        help="Deprecated and ignored; no minimum benign-flow threshold is applied",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Deprecated and ignored; client sampling is no longer performed",
    )
    parser.add_argument("--calibration-good-flows", type=int, default=200)
    parser.add_argument("--window-size", type=int, default=200)
    parser.add_argument("--min-estimator-samples", type=int, default=30)
    parser.add_argument("--refit-every", type=int, default=10)
    parser.add_argument("--speedup", type=float, default=0.0, help="0 removes wall-clock pacing")
    parser.add_argument("--max-evaluation-flows", type=int, default=0, help="0 means all evaluation flows")
    parser.add_argument(
        "--max-attempts",
        "--max-linear-power-attempts",
        dest="max_attempts",
        type=int,
        default=64,
        help=(
            "Maximum number of attempts allowed for a good LINEAR-POWER flow. "
            "LINEAR is the zero-latency baseline and always completes in one attempt. "
            "--max-linear-power-attempts is retained as a backward-compatible alias."
        ),
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=0.01,
        help="Logical trace seconds between a rejection response and the next good-flow attempt",
    )
    parser.add_argument(
        "--good-flow-timeout",
        type=float,
        default=60.0,
        help="Logical trace seconds allowed for a good flow to complete; 0 disables the timeout",
    )
    parser.add_argument(
        "--socket-timeout",
        type=float,
        default=10.0,
        help="Real wall-clock timeout for one local socket request",
    )
    args = parser.parse_args()

    if args.retry_delay < 0:
        raise ValueError("--retry-delay must be non-negative")
    if args.good_flow_timeout < 0:
        raise ValueError("--good-flow-timeout must be non-negative")
    if args.socket_timeout <= 0:
        raise ValueError("--socket-timeout must be positive")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_df = load_trace(Path(args.csv))
    # No protocol filtering and no client sampling/capping are performed.
    # Every labeled flow with a valid timestamp is retained regardless of its
    # original IP Protocol value.  The earliest calibration-good-flows BENIGN
    # rows are used to initialize the estimator, but they are ALSO replayed in
    # the measured workload.  In other words, calibration does not delete any
    # otherwise-valid flow from evaluation.
    selected_df = raw_df.copy()

    calibration = selected_df.loc[selected_df["is_good"]].head(args.calibration_good_flows).copy()
    if len(calibration) < args.calibration_good_flows:
        raise ValueError(
            f"Only {len(calibration)} benign flows are available in the trace for calibration; "
            f"requested {args.calibration_good_flows}"
        )
    calibration_ids = set(map(str, calibration["flow_uid"]))
    evaluation = selected_df.copy()
    evaluation = evaluation.sort_values(["parsed_timestamp", "flow_uid"], kind="stable")
    if args.max_evaluation_flows > 0:
        evaluation = evaluation.head(args.max_evaluation_flows)

    all_source_ips = sorted(map(str, selected_df["Source IP"].dropna().unique()))
    evaluation_source_ips = sorted(map(str, evaluation["Source IP"].dropna().unique()))
    benign_source_ips = sorted(map(str, selected_df.loc[selected_df["is_good"], "Source IP"].dropna().unique()))
    attack_source_ips = sorted(map(str, selected_df.loc[~selected_df["is_good"], "Source IP"].dropna().unique()))

    selection_summary: dict[str, Any] = {
            "client_selection_policy": "all source IPs; no benign-client sampling or minimum-flow threshold",
            "mode": args.mode,
            "algorithm": args.algorithm,
            "csv": str(Path(args.csv).resolve()),
            "source_csv_rows_after_label_timestamp_validation": int(len(raw_df)),
            "all_protocol_labeled_flow_count": int(len(selected_df)),
            "protocol_selection_policy": "all protocol values are included; no Protocol filter is applied",
            "replay_transport_policy": (
                "original protocol 17 uses UDP; original protocol 6 uses TCP; "
                "all other protocol values are carried over the TCP replay socket while preserving the original Protocol metadata"
            ),
            "calibration_good_flow_count": int(len(calibration)),
            "calibration_flows_retained_in_evaluation": True,
            "evaluation_workload_policy": (
                "all valid labeled flows are replayed; the benign calibration flows are retained in evaluation"
            ),
            "evaluation_flow_count": int(len(evaluation)),
            "all_unique_source_ip_count": int(len(all_source_ips)),
            "evaluation_unique_source_ip_count": int(len(evaluation_source_ips)),
            "source_ips_with_benign_flows": int(len(benign_source_ips)),
            "source_ips_with_attack_flows": int(len(attack_source_ips)),
            "protocol_counts_selected": {str(k): int(v) for k, v in selected_df["Protocol"].value_counts().items()},
            "label_counts_selected": {str(k): int(v) for k, v in selected_df["Label"].value_counts().items()},
            "retry_scheduler": (
                "LINEAR uses the zero-latency one-attempt baseline; LINEAR-POWER uses a "
                "deterministic discrete-event retry scheduler in which rejected good flows "
                "are reinserted at attempt_time + retry_delay"
            ),
            "retry_delay_trace_seconds": args.retry_delay if args.algorithm == "linear-power" else None,
            "good_flow_timeout_trace_seconds": args.good_flow_timeout,
            "good_flow_fee_policy": (
                "LINEAR: exact current price, one attempt, no rejected fee; "
                "LINEAR-POWER: only the final accepted fee is charged and rejected attached fees are not charged"
            ),
            "bad_flow_fee_policy": "all submitted bad-flow fees remain part of adversary cost",
            "pricing_scope": "single global server state shared by all destinations and original protocol values",
            "destination_ip_role": "trace metadata only; does not select a pricing state",
            "same_time_tie_rule": "new dataset arrivals are processed before retries at the same logical time",
        }
    (output_dir / "selection_summary.json").write_text(json.dumps(selection_summary, indent=2), encoding="utf-8")

    manifest_columns = [
        "flow_uid",
        "Timestamp",
        "Source IP",
        "Source Port",
        "Destination IP",
        "Destination Port",
        "Protocol",
        "Label",
    ]
    # evaluation_ground_truth.csv is an intermediate required by evaluate.py
    # and by the automatic Theorem 2 M calculation.  It is deleted after a
    # successful run by run_experiment.py.
    evaluation[manifest_columns].to_csv(output_dir / "evaluation_ground_truth.csv", index=False)

    estimator_client = JsonLineClient(args.estimator_host, args.estimator_port, timeout=args.socket_timeout)
    control_client = JsonLineClient(args.server_host, args.control_port, timeout=args.socket_timeout)

    reset = estimator_client.request(
        {
            "command": "reset",
            "mode": args.mode,
            "window_size": args.window_size,
            "min_samples": args.min_estimator_samples,
            "refit_every": args.refit_every,
        }
    )
    if not reset.get("ok"):
        raise RuntimeError(reset)

    calibration_pacer = TracePacer(args.speedup)
    for _, row in calibration.iterrows():
        calibration_pacer.wait(float(row["parsed_timestamp"].timestamp()))
        response = estimator_client.request(
            {
                "command": "observe",
                "trace_time": float(row["parsed_timestamp"].timestamp()),
                "label": str(row["Label"]).strip(),
            }
        )
        if not response.get("ok"):
            raise RuntimeError(response)

    frozen = estimator_client.request({"command": "freeze"})
    if not frozen.get("ok"):
        raise RuntimeError(frozen)
    initial_estimate = frozen["estimate"]

    start_eval = estimator_client.request({"command": "start_evaluation"})
    if not start_eval.get("ok"):
        raise RuntimeError(start_eval)
    server_reset = control_client.request(
        {
            "command": "reset",
            "iteration_length": initial_estimate["iteration_length"],
            "version": initial_estimate["version"],
            "source": initial_estimate["source"],
        }
    )
    if not server_reset.get("ok"):
        raise RuntimeError(server_reset)

    controller_log = CsvAppender(
        output_dir / "controller_replay_log.csv",
        [
            "sequence_id",
            "flow_uid",
            "timestamp",
            "source_ip",
            "destination_ip",
            "destination_port",
            "protocol",
            "label",
            "algorithm",
            "status",
            "price",
            "attached_fee",
            "accepted_fee",
            "submitted_fee_sum",
            "charged_fee",
            "total_fee_paid",
            "attempt_count",
            "rejection_count",
            "bounced_attempts",
            "generated_trace_time",
            "completion_trace_time",
            "completion_latency_trace_seconds",
            "completion_latency_wall_seconds",
            "deadline_trace_time",
            "completed_before_timeout",
            "timed_out",
            "iteration_id",
            "iteration_length",
            "estimator_update_version",
            "error",
        ],
    )
    good_metrics_log = CsvAppender(
        output_dir / "good_flow_completion_metrics.csv",
        [
            "sequence_id",
            "flow_uid",
            "source_ip",
            "destination_ip",
            "protocol",
            "generated_trace_time",
            "completion_trace_time",
            "completion_latency_trace_seconds",
            "completion_latency_wall_seconds",
            "timeout_seconds",
            "deadline_trace_time",
            "status",
            "completed_before_timeout",
            "timed_out",
            "attempt_count",
            "rejection_count",
            "final_accepted_fee",
            "final_required_price",
            "rejected_fee_sum_not_charged",
            "error",
        ],
    )

    # Every source IP with at least one evaluation flow is a logical client.
    clients = {
        ip: TraceClient(ip, args.server_host, args.udp_port, args.tcp_port, timeout=args.socket_timeout)
        for ip in evaluation_source_ips
    }
    pacer = TracePacer(args.speedup)
    retry_heap: list[tuple[float, int, str]] = []
    active: dict[str, FlowRuntime] = {}
    terminal_results: list[dict[str, Any]] = []
    heap_order = 0

    eval_iterator = enumerate(evaluation.iterrows(), start=1)
    try:
        next_initial = next(eval_iterator, None)

        def finalize(
            runtime: FlowRuntime,
            *,
            status: str,
            event_time: float | None,
            ack: dict[str, Any] | None = None,
            error: str = "",
            timed_out: bool = False,
        ) -> None:
            if runtime.terminal:
                return
            runtime.terminal = True
            ack = ack or {}
            terminal_wall_latency = (
                time.perf_counter() - runtime.first_wall_time
                if runtime.first_wall_time is not None
                else 0.0
            )
            completed = status == "ok" and bool(ack.get("serviced", True))
            completion_trace_time = float(event_time) if completed and event_time is not None else ""
            completion_latency_trace = (
                float(event_time) - runtime.generated_trace_time
                if completed and event_time is not None
                else ""
            )
            accepted_fee = float(ack.get("accepted_fee", ack.get("attached_fee", 0.0))) if completed else 0.0
            submitted_fee_sum = float(ack.get("submitted_fee_sum", runtime.submitted_fee_sum))
            charged_fee = accepted_fee if runtime.is_good else submitted_fee_sum
            deadline = runtime.deadline_trace_time
            completed_before_timeout = bool(
                completed and (deadline is None or (event_time is not None and event_time <= deadline))
            )
            result = {
                "sequence_id": runtime.sequence_id,
                "flow_uid": runtime.row["flow_uid"],
                "timestamp": runtime.row["parsed_timestamp"].isoformat(),
                "source_ip": runtime.row["Source IP"],
                "destination_ip": runtime.row["Destination IP"],
                "destination_port": int(runtime.row["Destination Port"]),
                "protocol": int(runtime.row["Protocol"]),
                "label": runtime.row["Label"],
                "algorithm": args.algorithm,
                "status": status,
                "price": ack.get("price", runtime.last_required_price or ""),
                "attached_fee": ack.get("attached_fee", runtime.last_attached_fee or ""),
                "accepted_fee": accepted_fee if completed else "",
                "submitted_fee_sum": submitted_fee_sum,
                "charged_fee": charged_fee if completed else 0.0,
                "total_fee_paid": charged_fee if completed else 0.0,
                "attempt_count": int(ack.get("attempt_count", runtime.attempt_count)),
                "rejection_count": int(ack.get("rejection_count", runtime.rejection_count)),
                "bounced_attempts": int(ack.get("rejection_count", runtime.rejection_count)),
                "generated_trace_time": runtime.generated_trace_time,
                "completion_trace_time": completion_trace_time,
                "completion_latency_trace_seconds": completion_latency_trace,
                "completion_latency_wall_seconds": terminal_wall_latency if completed else "",
                "deadline_trace_time": deadline if deadline is not None else "",
                "completed_before_timeout": completed_before_timeout,
                "timed_out": timed_out,
                "iteration_id": ack.get("iteration_id", ""),
                "iteration_length": ack.get("iteration_length", ""),
                "estimator_update_version": runtime.estimator_update_version,
                "error": error,
            }
            runtime.result = result
            terminal_results.append(result)
            active.pop(str(runtime.row["flow_uid"]), None)
            if not completed:
                try:
                    control_client.request({"command": "abandon_flow", "flow_uid": str(runtime.row["flow_uid"])})
                except Exception:
                    pass

        def observe_initial(runtime: FlowRuntime) -> None:
            if args.mode != "sliding":
                return
            # The calibration rows are replayed for pricing, but they were
            # already consumed by the estimator during calibration.  Do not
            # feed those same benign observations into the sliding estimator a
            # second time.  estimator_service.start_evaluation() retains the
            # final calibration good timestamp so the first post-calibration
            # benign flow still forms the correct next IAT.
            if str(runtime.row["flow_uid"]) in calibration_ids:
                return
            trace_time = runtime.generated_trace_time
            estimator_response = estimator_client.request(
                {
                    "command": "observe",
                    "trace_time": trace_time,
                    "label": str(runtime.row["Label"]).strip(),
                }
            )
            if not estimator_response.get("ok"):
                raise RuntimeError(estimator_response)
            if estimator_response.get("updated"):
                estimate = estimator_response["estimate"]
                update = control_client.request(
                    {
                        "command": "set_iteration_length",
                        "iteration_length": estimate["iteration_length"],
                        "version": estimate["version"],
                        "source": estimate["source"],
                    }
                )
                if not update.get("ok"):
                    raise RuntimeError(update)
                runtime.estimator_update_version = estimate["version"]

        while next_initial is not None or retry_heap:
            next_initial_time = math.inf
            if next_initial is not None:
                _, (_, next_row) = next_initial
                next_initial_time = float(next_row["parsed_timestamp"].timestamp())
            next_retry_time = retry_heap[0][0] if retry_heap else math.inf

            # New arrivals win exact-time ties so a rejected flow does not
            # immediately reclaim the server ahead of flows already arriving.
            is_initial_event = next_initial_time <= next_retry_time
            if is_initial_event:
                sequence_id, (_, row) = next_initial
                generated_time = float(row["parsed_timestamp"].timestamp())
                deadline = (
                    generated_time + args.good_flow_timeout
                    if bool(row["is_good"]) and args.good_flow_timeout > 0
                    else None
                )
                runtime = FlowRuntime(
                    sequence_id=sequence_id,
                    row=row,
                    base_message=flow_message(row, sequence_id),
                    is_good=bool(row["is_good"]),
                    generated_trace_time=generated_time,
                    deadline_trace_time=deadline,
                )
                active[str(row["flow_uid"])] = runtime
                event_time = generated_time
                next_initial = next(eval_iterator, None)
            else:
                event_time, _, flow_uid = heapq.heappop(retry_heap)
                runtime = active.get(flow_uid)
                if runtime is None or runtime.terminal:
                    continue

            pacer.wait(event_time)
            if runtime.first_wall_time is None:
                runtime.first_wall_time = time.perf_counter()

            if runtime.is_good and runtime.deadline_trace_time is not None and event_time > runtime.deadline_trace_time:
                finalize(
                    runtime,
                    status="timed_out",
                    event_time=None,
                    error="good-flow logical deadline expired before the next attempt",
                    timed_out=True,
                )
                continue

            client = clients[str(runtime.row["Source IP"])]
            ack: dict[str, Any] = {}
            error = ""
            try:
                if args.algorithm == "linear":
                    # Zero-latency LINEAR: one atomic request.  The server
                    # assigns/charges the exact current price, so there is no
                    # stale-fee rejection or retry scheduling for LINEAR.
                    message = attempt_message(runtime, event_time, fee=None)
                    runtime.attempt_count = 1
                    ack = client.send_once(message)
                    runtime.last_required_price = float(ack.get("price", 0.0)) if ack.get("ok") else None
                    runtime.last_attached_fee = float(ack.get("attached_fee", ack.get("price", 0.0))) if ack.get("ok") else None
                    runtime.submitted_fee_sum = float(ack.get("submitted_fee_sum", runtime.last_attached_fee or 0.0))
                else:
                    # Asynchronous LINEAR-POWER. Good flows begin at fee 1
                    # and may retry after a bounce. Bad flows use the informed
                    # minimum-fee strategy by quoting the current global price.
                    if runtime.is_good:
                        fee = runtime.current_fee
                    else:
                        quote = control_client.request(
                            {
                                "command": "quote",
                                "trace_time": event_time,
                            }
                        )
                        if not quote.get("ok"):
                            raise RuntimeError(quote)
                        fee = float(quote["price"])
                    message = attempt_message(runtime, event_time, fee=fee)
                    runtime.attempt_count += 1
                    runtime.last_attached_fee = fee
                    runtime.submitted_fee_sum += fee
                    ack = client.send_once(message)

                if not ack.get("ok") or ack.get("skipped"):
                    error = str(ack.get("reason") or ack.get("error") or "request failed")
                    finalize(runtime, status="error", event_time=None, ack=ack, error=error)
                elif ack.get("serviced", True):
                    runtime.last_required_price = float(ack.get("price", 0.0))
                    finalize(runtime, status="ok", event_time=event_time, ack=ack)
                else:
                    runtime.rejection_count = int(ack.get("rejection_count", runtime.rejection_count + 1))
                    runtime.last_required_price = float(ack["price"])
                    if not runtime.is_good:
                        finalize(
                            runtime,
                            status="error",
                            event_time=None,
                            ack=ack,
                            error="bad flow was rejected despite exact-price quote",
                        )
                    elif runtime.attempt_count >= args.max_attempts:
                        finalize(
                            runtime,
                            status="max_attempts",
                            event_time=None,
                            ack=ack,
                            error=f"exceeded {args.max_attempts} attempts",
                        )
                    else:
                        runtime.current_fee = max(runtime.current_fee, float(ack["price"]))
                        next_retry = event_time + args.retry_delay
                        if runtime.deadline_trace_time is not None and next_retry > runtime.deadline_trace_time:
                            finalize(
                                runtime,
                                status="timed_out",
                                event_time=None,
                                ack=ack,
                                error="next retry would occur after the good-flow logical deadline",
                                timed_out=True,
                            )
                        else:
                            heap_order += 1
                            heapq.heappush(
                                retry_heap,
                                (next_retry, heap_order, str(runtime.row["flow_uid"])),
                            )
            except Exception as exc:
                finalize(
                    runtime,
                    status="error",
                    event_time=None,
                    ack=ack,
                    error=f"{type(exc).__name__}: {exc}",
                )

            if is_initial_event:
                observe_initial(runtime)

        for result in sorted(terminal_results, key=lambda item: int(item["sequence_id"])):
            controller_log.write(result)
            if str(result["label"]).strip().casefold() == "benign":
                good_metrics_log.write(
                    {
                        "sequence_id": result["sequence_id"],
                        "flow_uid": result["flow_uid"],
                        "source_ip": result["source_ip"],
                        "destination_ip": result["destination_ip"],
                        "protocol": result["protocol"],
                        "generated_trace_time": result["generated_trace_time"],
                        "completion_trace_time": result["completion_trace_time"],
                        "completion_latency_trace_seconds": result["completion_latency_trace_seconds"],
                        "completion_latency_wall_seconds": result["completion_latency_wall_seconds"],
                        "timeout_seconds": args.good_flow_timeout if args.good_flow_timeout > 0 else "",
                        "deadline_trace_time": result["deadline_trace_time"],
                        "status": result["status"],
                        "completed_before_timeout": result["completed_before_timeout"],
                        "timed_out": result["timed_out"],
                        "attempt_count": result["attempt_count"],
                        "rejection_count": result["rejection_count"],
                        "final_accepted_fee": result["accepted_fee"],
                        "final_required_price": result["price"],
                        "rejected_fee_sum_not_charged": (
                            float(result["submitted_fee_sum"]) - float(result["accepted_fee"])
                            if result["status"] == "ok" and result["accepted_fee"] != ""
                            else float(result["submitted_fee_sum"])
                        ),
                        "error": result["error"],
                    }
                )
    finally:
        for client in clients.values():
            client.close()
        estimator_client.close()
        control_client.close()
        controller_log.close()
        good_metrics_log.close()

    print(json.dumps({"ok": True, **selection_summary, "initial_estimate": initial_estimate}, indent=2))


if __name__ == "__main__":
    main()
