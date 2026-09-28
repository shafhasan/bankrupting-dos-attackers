from __future__ import annotations

import argparse
import csv
import heapq
import json
import math
import socket
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

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


CALIBRATION_GOOD_FLOWS = 50
SLIDING_WINDOW_SIZE = 50


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
    row: dict[str, Any]
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


OPTIONAL_FLOW_COLUMNS = {
    "Flow Duration",
    "Total Fwd Packets",
    "Total Backward Packets",
    "Total Length of Fwd Packets",
    "Total Length of Bwd Packets",
    "Unnamed: 0",
}


@dataclass
class TraceAnalysis:
    calibration: list[dict[str, Any]]
    calibration_prefix_flow_count: int
    calibration_cutoff_timestamp: str | None
    calibration_cutoff_flow_uid: str | None
    source_valid_rows: int
    evaluation_flow_count: int
    evaluation_good_count: int
    all_source_ips: set[str]
    evaluation_source_ips: set[str]
    benign_source_ips: set[str]
    attack_source_ips: set[str]
    selected_protocol_counts: dict[str, int]
    selected_label_counts: dict[str, int]
    evaluation_protocol_counts: dict[str, int]
    theorem2_m: int
    source_sorted: bool


def _increment(counter: dict[str, int], key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def _csv_header_mapping(csv_path: Path) -> dict[str, str]:
    raw_columns = list(pd.read_csv(csv_path, nrows=0).columns)
    stripped = strip_columns(raw_columns)
    mapping: dict[str, str] = {}
    for raw, clean in zip(raw_columns, stripped):
        if clean in mapping:
            raise ValueError(f"CSV has duplicate columns after whitespace stripping: {clean!r}")
        mapping[clean] = str(raw)
    missing = REQUIRED_COLUMNS.difference(mapping)
    if missing:
        raise ValueError(f"CSV is missing required columns: {sorted(missing)}")
    return mapping


def _unnamed_zero_is_unique(csv_path: Path, header_map: dict[str, str]) -> bool:
    """Match the old flow_uid policy without loading the full feature table.

    The previous implementation used ``Unnamed: 0`` only when that entire
    column was globally unique. Reading one scalar column is small compared
    with loading all CICDDoS feature columns and preserves that behavior.
    """
    raw = header_map.get("Unnamed: 0")
    if raw is None:
        return False
    one_column = pd.read_csv(csv_path, usecols=[raw], low_memory=False)[raw]
    unique = bool(one_column.is_unique)
    del one_column
    return unique


def _normalized_chunks(
    csv_path: Path,
    *,
    chunk_size: int,
    use_unnamed_uid: bool,
    header_map: dict[str, str],
) -> Iterator[pd.DataFrame]:
    wanted_clean = sorted(REQUIRED_COLUMNS | OPTIONAL_FLOW_COLUMNS)
    present_clean = [name for name in wanted_clean if name in header_map]
    raw_usecols = [header_map[name] for name in present_clean]
    original_row_offset = 0

    for raw_chunk in pd.read_csv(
        csv_path,
        usecols=raw_usecols,
        chunksize=chunk_size,
        low_memory=False,
    ):
        raw_len = len(raw_chunk)
        chunk = raw_chunk.copy()
        chunk.columns = strip_columns(chunk.columns)
        chunk["_original_row_index"] = range(
            original_row_offset, original_row_offset + raw_len
        )
        original_row_offset += raw_len

        for column in ("Source IP", "Destination IP", "Timestamp", "Label"):
            chunk[column] = (
                chunk[column]
                .astype("string")
                .str.replace("\ufeff", "", regex=False)
                .str.strip()
            )

        missing_label = chunk["Label"].isna() | chunk["Label"].eq("").fillna(False)
        if missing_label.any():
            chunk = chunk.loc[~missing_label].copy()
        if chunk.empty:
            continue

        is_good = (
            chunk["Label"]
            .astype("string")
            .str.casefold()
            .eq("benign")
            .fillna(False)
            .astype(bool)
        )
        chunk["is_good"] = is_good
        chunk.loc[is_good, "Label"] = BENIGN_LABEL

        try:
            parsed = pd.to_datetime(chunk["Timestamp"], errors="coerce", format="mixed")
        except (TypeError, ValueError):
            parsed = pd.to_datetime(chunk["Timestamp"], errors="coerce")
        valid_ts = parsed.notna()
        if not valid_ts.all():
            chunk = chunk.loc[valid_ts].copy()
            parsed = parsed.loc[valid_ts]
        if chunk.empty:
            continue
        chunk["parsed_timestamp"] = parsed

        chunk["Protocol"] = pd.to_numeric(chunk["Protocol"], errors="coerce").fillna(-1).astype(int)
        chunk["Source Port"] = pd.to_numeric(chunk["Source Port"], errors="coerce").fillna(0).astype(int)
        chunk["Destination Port"] = pd.to_numeric(chunk["Destination Port"], errors="coerce").fillna(0).astype(int)

        if use_unnamed_uid:
            chunk["flow_uid"] = chunk["Unnamed: 0"].map(lambda x: f"row-{x}")
        else:
            chunk["flow_uid"] = chunk["_original_row_index"].map(lambda x: f"row-{x}")

        yield chunk


def iter_normalized_rows(
    csv_path: Path,
    *,
    chunk_size: int,
    use_unnamed_uid: bool,
    header_map: dict[str, str],
) -> Iterator[dict[str, Any]]:
    """Yield valid trace rows with bounded memory in original file order."""
    for chunk in _normalized_chunks(
        csv_path,
        chunk_size=chunk_size,
        use_unnamed_uid=use_unnamed_uid,
        header_map=header_map,
    ):
        columns = list(chunk.columns)
        for values in chunk.itertuples(index=False, name=None):
            yield dict(zip(columns, values))


def _row_sort_key(row: dict[str, Any]) -> tuple[int, str]:
    ts = row["parsed_timestamp"]
    return int(ts.value), str(row["flow_uid"])


def analyze_trace(
    row_factory: Callable[[], Iterator[dict[str, Any]]],
    *,
    max_evaluation_flows: int,
    theorem2_delta: float,
) -> TraceAnalysis:
    """One bounded-memory pass over the sorted logical trace.

    The first 50 good flows are used for estimator calibration.  The entire
    chronological prefix through and including the 50th good flow is excluded
    from evaluation.  Evaluation therefore starts at the next flow in the
    globally timestamp/flow_uid ordered trace, after which an optional
    max-evaluation-flow cap is applied.
    """
    calibration: list[dict[str, Any]] = []
    calibration_prefix_flow_count = 0
    calibration_cutoff_timestamp: str | None = None
    calibration_cutoff_flow_uid: str | None = None
    source_valid_rows = 0
    evaluation_flow_count = 0
    evaluation_good_count = 0
    all_source_ips: set[str] = set()
    evaluation_source_ips: set[str] = set()
    benign_source_ips: set[str] = set()
    attack_source_ips: set[str] = set()
    selected_protocol_counts: dict[str, int] = {}
    selected_label_counts: dict[str, int] = {}
    evaluation_protocol_counts: dict[str, int] = {}
    source_sorted = True
    previous_key: tuple[int, str] | None = None

    # Since rows are sorted by time, a deque gives Theorem 2 M in O(g) time
    # and O(number of good arrivals in one Delta window) memory.
    delta_ns = max(1, int(round(theorem2_delta * 1_000_000_000.0))) if theorem2_delta > 0 else 0
    good_window: deque[int] = deque()
    theorem2_m = 1

    for row in row_factory():
        source_valid_rows += 1
        key = _row_sort_key(row)
        if previous_key is not None and key < previous_key:
            source_sorted = False
        previous_key = key

        source_ip = str(row["Source IP"])
        all_source_ips.add(source_ip)
        if bool(row["is_good"]):
            benign_source_ips.add(source_ip)
        else:
            attack_source_ips.add(source_ip)
        _increment(selected_protocol_counts, str(int(row["Protocol"])))
        _increment(selected_label_counts, str(row["Label"]))

        # Calibration is a chronological prefix, not a sparse set of 50 rows.
        # Keep the first 50 good rows for fitting, but exclude *every* flow
        # (good or bad) through and including the 50th good flow from the
        # measured workload.  This prevents replaying earlier attack traffic
        # with an estimator that was fitted using later good arrivals.
        if len(calibration) < CALIBRATION_GOOD_FLOWS:
            calibration_prefix_flow_count += 1
            if bool(row["is_good"]):
                calibration.append(dict(row))
                if len(calibration) == CALIBRATION_GOOD_FLOWS:
                    calibration_cutoff_timestamp = row["parsed_timestamp"].isoformat()
                    calibration_cutoff_flow_uid = str(row["flow_uid"])
            continue

        if max_evaluation_flows > 0 and evaluation_flow_count >= max_evaluation_flows:
            continue

        evaluation_flow_count += 1
        evaluation_source_ips.add(source_ip)
        _increment(evaluation_protocol_counts, str(int(row["Protocol"])))
        if bool(row["is_good"]):
            evaluation_good_count += 1
            if theorem2_delta > 0:
                t_ns = int(row["parsed_timestamp"].value)
                good_window.append(t_ns)
                while good_window and t_ns - good_window[0] > delta_ns:
                    good_window.popleft()
                theorem2_m = max(theorem2_m, len(good_window))

    return TraceAnalysis(
        calibration=calibration,
        calibration_prefix_flow_count=calibration_prefix_flow_count,
        calibration_cutoff_timestamp=calibration_cutoff_timestamp,
        calibration_cutoff_flow_uid=calibration_cutoff_flow_uid,
        source_valid_rows=source_valid_rows,
        evaluation_flow_count=evaluation_flow_count,
        evaluation_good_count=evaluation_good_count,
        all_source_ips=all_source_ips,
        evaluation_source_ips=evaluation_source_ips,
        benign_source_ips=benign_source_ips,
        attack_source_ips=attack_source_ips,
        selected_protocol_counts=selected_protocol_counts,
        selected_label_counts=selected_label_counts,
        evaluation_protocol_counts=evaluation_protocol_counts,
        theorem2_m=max(1, theorem2_m),
        source_sorted=source_sorted,
    )


def iter_evaluation_rows(
    row_factory: Callable[[], Iterator[dict[str, Any]]],
    *,
    max_evaluation_flows: int,
) -> Iterator[dict[str, Any]]:
    """Yield only rows strictly after the 50th good calibration flow."""
    calibration_good_seen = 0
    emitted = 0
    for row in row_factory():
        if calibration_good_seen < CALIBRATION_GOOD_FLOWS:
            if bool(row["is_good"]):
                calibration_good_seen += 1
            # The 50th good row itself is part of the calibration prefix, so it
            # is also skipped.  The next chronological row is the first
            # evaluation row.
            continue
        if max_evaluation_flows > 0 and emitted >= max_evaluation_flows:
            return
        emitted += 1
        yield row


def _serialize_sort_row(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["parsed_timestamp_ns"] = int(row["parsed_timestamp"].value)
    result.pop("parsed_timestamp", None)
    result["is_good"] = "1" if bool(row["is_good"]) else "0"
    return result


def _deserialize_sort_row(row: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = dict(row)
    out["parsed_timestamp"] = pd.Timestamp(int(out.pop("parsed_timestamp_ns")))
    out["is_good"] = str(out["is_good"]) == "1"
    for name in ("Protocol", "Source Port", "Destination Port", "_original_row_index"):
        out[name] = int(float(out.get(name, 0) or 0))
    for name in (
        "Flow Duration", "Total Fwd Packets", "Total Backward Packets",
        "Total Length of Fwd Packets", "Total Length of Bwd Packets",
    ):
        if name in out and out[name] != "":
            try:
                out[name] = float(out[name])
            except (TypeError, ValueError):
                out[name] = 0.0
    return out


def materialize_external_sort(
    csv_path: Path,
    *,
    chunk_size: int,
    use_unnamed_uid: bool,
    header_map: dict[str, str],
    temp_dir: Path,
) -> Path:
    """External merge sort fallback for an input file that is not already ordered.

    Normal CICDDoS traces are already timestamp ordered, so this path is not
    used in the common case. When needed, only the replay columns are written
    to temporary files; they are removed automatically after the run.
    """
    run_paths: list[Path] = []
    fieldnames: list[str] | None = None
    for run_index, chunk in enumerate(_normalized_chunks(
        csv_path,
        chunk_size=chunk_size,
        use_unnamed_uid=use_unnamed_uid,
        header_map=header_map,
    )):
        chunk = chunk.sort_values(["parsed_timestamp", "flow_uid"], kind="stable")
        rows = [_serialize_sort_row(dict(zip(chunk.columns, values))) for values in chunk.itertuples(index=False, name=None)]
        if not rows:
            continue
        if fieldnames is None:
            fieldnames = list(rows[0].keys())
        run_path = temp_dir / f"run_{run_index:06d}.csv"
        with run_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        run_paths.append(run_path)

    if not run_paths or fieldnames is None:
        raise ValueError("No valid labeled/timestamped rows remain after input validation")

    merged = temp_dir / "trace_sorted.csv"
    handles = [path.open("r", newline="", encoding="utf-8") for path in run_paths]
    readers = [csv.DictReader(handle) for handle in handles]
    heap: list[tuple[int, str, int, dict[str, str]]] = []
    try:
        for idx, reader in enumerate(readers):
            row = next(reader, None)
            if row is not None:
                heapq.heappush(heap, (int(row["parsed_timestamp_ns"]), str(row["flow_uid"]), idx, row))
        with merged.open("w", newline="", encoding="utf-8") as out_handle:
            writer = csv.DictWriter(out_handle, fieldnames=fieldnames)
            writer.writeheader()
            while heap:
                _, _, idx, row = heapq.heappop(heap)
                writer.writerow(row)
                nxt = next(readers[idx], None)
                if nxt is not None:
                    heapq.heappush(heap, (int(nxt["parsed_timestamp_ns"]), str(nxt["flow_uid"]), idx, nxt))
    finally:
        for handle in handles:
            handle.close()
        for path in run_paths:
            path.unlink(missing_ok=True)
    return merged


def iter_sorted_materialized_rows(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            yield _deserialize_sort_row(row)

def safe_number(row: dict[str, Any], column: str, default: float = 0.0) -> float:
    value = row.get(column, default)
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def flow_message(row: dict[str, Any], sequence_id: int) -> dict[str, Any]:
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




PLOT_EVERY = 500


@dataclass
class RunningPearson:
    """Numerically stable online Pearson correlation over all valid points."""

    n: int = 0
    mean_x: float = 0.0
    mean_y: float = 0.0
    m2_x: float = 0.0
    m2_y: float = 0.0
    covariance_sum: float = 0.0

    def update(self, x: float, y: float) -> None:
        if not (math.isfinite(x) and math.isfinite(y)):
            return
        self.n += 1
        dx = x - self.mean_x
        self.mean_x += dx / self.n
        dy = y - self.mean_y
        self.mean_y += dy / self.n
        self.m2_x += dx * (x - self.mean_x)
        self.m2_y += dy * (y - self.mean_y)
        self.covariance_sum += dx * (y - self.mean_y)

    def value(self) -> float | None:
        if self.n < 2 or self.m2_x <= 0.0 or self.m2_y <= 0.0:
            return None
        denominator = math.sqrt(self.m2_x * self.m2_y)
        if denominator <= 0.0 or not math.isfinite(denominator):
            return None
        value = self.covariance_sum / denominator
        return float(value) if math.isfinite(value) else None


def theorem_proxy_value(
    *,
    algorithm: str,
    adversary_cost_b: float,
    cumulative_good_jobs: int,
    theorem2_m: float,
) -> float:
    """Return the raw, unscaled theorem proxy at the current serviced-flow point."""
    b = float(adversary_cost_b)
    g_plus_one = float(cumulative_good_jobs + 1)
    if algorithm == "linear-power":
        min_term = min(
            g_plus_one,
            theorem2_m * math.sqrt(g_plus_one),
            theorem2_m * math.sqrt(b + 1.0),
        )
        denominator = math.sqrt(b + 1.0) * min_term + g_plus_one
    else:
        denominator = math.sqrt(b * g_plus_one) + g_plus_one
    return float(b / denominator) if denominator > 0.0 else 0.0


def safe_mean_total(total: float, count: int) -> float | None:
    return float(total / count) if count > 0 else None


def percentile_95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = 0.95 * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return float(ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction)


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
    parser.add_argument(
        "--benign-client-count", type=int, default=None,
        help="Deprecated and ignored; all clients are included",
    )
    parser.add_argument(
        "--min-benign-flows-per-client", type=int, default=None,
        help="Deprecated and ignored; no minimum benign-flow threshold is applied",
    )
    parser.add_argument(
        "--seed", type=int, default=None,
        help="Deprecated and ignored; client sampling is no longer performed",
    )
    parser.add_argument(
        "--calibration-good-flows", type=int, default=50,
        help="Deprecated and ignored; calibration always uses the first 50 good flows",
    )
    parser.add_argument(
        "--window-size", type=int, default=50,
        help="Deprecated and ignored; sliding-window size is fixed at 50",
    )
    parser.add_argument("--min-estimator-samples", type=int, default=30)
    parser.add_argument("--refit-every", type=int, default=10)
    parser.add_argument("--speedup", type=float, default=0.0, help="0 removes wall-clock pacing")
    parser.add_argument("--max-evaluation-flows", type=int, default=0, help="0 means all evaluation flows")
    parser.add_argument(
        "--max-attempts", "--max-linear-power-attempts", dest="max_attempts",
        type=int, default=64,
        help=(
            "Maximum number of attempts allowed for a good LINEAR-POWER flow. "
            "LINEAR is the zero-latency baseline and always completes in one attempt."
        ),
    )
    parser.add_argument(
        "--retry-delay", type=float, default=0.01,
        help="Logical trace seconds between a rejection response and the next good-flow attempt",
    )
    parser.add_argument(
        "--good-flow-timeout", type=float, default=60.0,
        help="Logical trace seconds allowed for a good flow to complete; 0 disables the timeout",
    )
    parser.add_argument(
        "--socket-timeout", type=float, default=10.0,
        help="Real wall-clock timeout for one local socket request",
    )
    parser.add_argument(
        "--csv-chunk-size", type=int, default=100_000,
        help="Number of CSV rows processed at a time; bounds controller memory usage",
    )
    parser.add_argument(
        "--temp-dir", default=None,
        help="Optional directory for automatic external-sort temporary files",
    )
    parser.add_argument(
        "--theorem2-M", type=float, default=None,
        help=(
            "Optional manual Theorem 2 M override. Otherwise M is computed from "
            "post-calibration original good arrivals using --retry-delay as Delta proxy."
        ),
    )
    args = parser.parse_args()

    if args.retry_delay < 0:
        raise ValueError("--retry-delay must be non-negative")
    if args.good_flow_timeout < 0:
        raise ValueError("--good-flow-timeout must be non-negative")
    if args.socket_timeout <= 0:
        raise ValueError("--socket-timeout must be positive")
    if args.csv_chunk_size <= 0:
        raise ValueError("--csv-chunk-size must be positive")
    if args.theorem2_M is not None and args.theorem2_M <= 0:
        raise ValueError("--theorem2-M must be positive")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = Path(args.csv)

    # Low-memory input path: inspect only the required replay columns and process
    # the source in bounded chunks. This replaces the former full-table
    # pd.read_csv(...), full DataFrame copies, and full DataFrame sort.
    header_map = _csv_header_mapping(csv_path)
    use_unnamed_uid = _unnamed_zero_is_unique(csv_path, header_map)

    def original_row_factory() -> Iterator[dict[str, Any]]:
        return iter_normalized_rows(
            csv_path,
            chunk_size=args.csv_chunk_size,
            use_unnamed_uid=use_unnamed_uid,
            header_map=header_map,
        )

    row_factory: Callable[[], Iterator[dict[str, Any]]] = original_row_factory
    analysis = analyze_trace(
        row_factory,
        max_evaluation_flows=args.max_evaluation_flows,
        theorem2_delta=args.retry_delay,
    )

    # The old implementation globally sorted by (parsed_timestamp, flow_uid).
    # CICDDoS traces are normally already in that order. If an input is not,
    # preserve the old semantics with a bounded-memory external merge sort over
    # only the replay columns. Temporary runs disappear when this process exits.
    temp_sort_context: tempfile.TemporaryDirectory[str] | None = None
    input_ordering = "input already sorted by parsed_timestamp, flow_uid"
    if not analysis.source_sorted:
        temp_sort_context = tempfile.TemporaryDirectory(
            prefix="ddos_trace_sort_", dir=args.temp_dir
        )
        sorted_path = materialize_external_sort(
            csv_path,
            chunk_size=args.csv_chunk_size,
            use_unnamed_uid=use_unnamed_uid,
            header_map=header_map,
            temp_dir=Path(temp_sort_context.name),
        )

        def sorted_row_factory() -> Iterator[dict[str, Any]]:
            return iter_sorted_materialized_rows(sorted_path)

        row_factory = sorted_row_factory
        analysis = analyze_trace(
            row_factory,
            max_evaluation_flows=args.max_evaluation_flows,
            theorem2_delta=args.retry_delay,
        )
        if not analysis.source_sorted:
            raise RuntimeError("External sort failed to produce a globally ordered trace")
        input_ordering = "automatic external merge sort by parsed_timestamp, flow_uid"

    calibration = analysis.calibration
    if len(calibration) < CALIBRATION_GOOD_FLOWS:
        raise ValueError(
            f"Only {len(calibration)} benign flows are available in the trace for calibration; "
            f"requested {CALIBRATION_GOOD_FLOWS}"
        )

    all_source_ips = sorted(analysis.all_source_ips)
    evaluation_source_ips = sorted(analysis.evaluation_source_ips)
    benign_source_ips = sorted(analysis.benign_source_ips)
    attack_source_ips = sorted(analysis.attack_source_ips)
    evaluation_protocol_counts = dict(analysis.evaluation_protocol_counts)
    evaluation_good_count = int(analysis.evaluation_good_count)

    if args.algorithm == "linear-power":
        if args.theorem2_M is not None:
            theorem2_m = float(args.theorem2_M)
            theorem2_m_source = "manual override"
            theorem2_delta_proxy = args.retry_delay
        else:
            if args.retry_delay <= 0:
                raise ValueError(
                    "Cannot automatically compute Theorem 2 M with a non-positive retry delay. "
                    "Use a positive --retry-delay or supply --theorem2-M."
                )
            theorem2_m = float(analysis.theorem2_m)
            theorem2_m_source = "computed online from post-calibration evaluation good-flow arrivals"
            theorem2_delta_proxy = float(args.retry_delay)
    else:
        theorem2_m = 1.0
        theorem2_m_source = "not used by LINEAR"
        theorem2_delta_proxy = None

    selection_summary: dict[str, Any] = {
        "client_selection_policy": "all source IPs; no benign-client sampling or minimum-flow threshold",
        "mode": args.mode,
        "algorithm": args.algorithm,
        "csv": str(csv_path.resolve()),
        "source_csv_rows_after_label_timestamp_validation": int(analysis.source_valid_rows),
        "all_protocol_labeled_flow_count": int(analysis.source_valid_rows),
        "protocol_selection_policy": "all protocol values are included; no Protocol filter is applied",
        "replay_transport_policy": (
            "original protocol 17 uses UDP; original protocol 6 uses TCP; "
            "all other protocol values are carried over the TCP replay socket while preserving original Protocol metadata"
        ),
        "calibration_good_flow_count": int(len(calibration)),
        "calibration_prefix_flow_count": int(analysis.calibration_prefix_flow_count),
        "calibration_cutoff_timestamp": analysis.calibration_cutoff_timestamp,
        "calibration_cutoff_flow_uid": analysis.calibration_cutoff_flow_uid,
        "sliding_window_size": SLIDING_WINDOW_SIZE,
        "calibration_flows_retained_in_evaluation": False,
        "evaluation_workload_policy": (
            "the entire chronological prefix through and including the 50th good flow is calibration-only and removed from evaluation; replay starts with the next flow"
        ),
        "post_calibration_server_state_reset": True,
        "evaluation_flow_count": int(analysis.evaluation_flow_count),
        "all_unique_source_ip_count": int(len(all_source_ips)),
        "evaluation_unique_source_ip_count": int(len(evaluation_source_ips)),
        "source_ips_with_benign_flows": int(len(benign_source_ips)),
        "source_ips_with_attack_flows": int(len(attack_source_ips)),
        "protocol_counts_selected": dict(analysis.selected_protocol_counts),
        "label_counts_selected": dict(analysis.selected_label_counts),
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
        "large_intermediate_csvs": "disabled; accounting and Pearson correlation are computed online",
        "input_loading": "selective-column chunked streaming; no full dataset DataFrame",
        "csv_chunk_size": int(args.csv_chunk_size),
        "input_ordering": input_ordering,
        "flow_uid_policy": (
            "row-<Unnamed: 0> because Unnamed: 0 is globally unique"
            if use_unnamed_uid
            else "row-<original CSV row index>, matching the previous fallback policy"
        ),
        "plot_checkpoint_interval_flows": PLOT_EVERY,
    }
    (output_dir / "selection_summary.json").write_text(
        json.dumps(selection_summary, indent=2), encoding="utf-8"
    )

    estimator_client = JsonLineClient(
        args.estimator_host, args.estimator_port, timeout=args.socket_timeout
    )
    control_client = JsonLineClient(
        args.server_host, args.control_port, timeout=args.socket_timeout
    )

    reset = estimator_client.request(
        {
            "command": "reset",
            "mode": args.mode,
            "window_size": SLIDING_WINDOW_SIZE,
            "min_samples": args.min_estimator_samples,
            "refit_every": args.refit_every,
        }
    )
    if not reset.get("ok"):
        raise RuntimeError(reset)

    calibration_pacer = TracePacer(args.speedup)
    for row in calibration:
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

    # Explicit reset after calibration: iteration id/start, serviced counter,
    # pricing state and pending retry bookkeeping all start clean for evaluation.
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

    good_metrics_log = CsvAppender(
        output_dir / "good_flow_completion_metrics.csv",
        [
            "sequence_id", "flow_uid", "source_ip", "destination_ip", "protocol",
            "generated_trace_time", "completion_trace_time",
            "completion_latency_trace_seconds", "completion_latency_wall_seconds",
            "timeout_seconds", "deadline_trace_time", "status",
            "completed_before_timeout", "timed_out", "attempt_count",
            "rejection_count", "final_accepted_fee", "final_required_price",
            "rejected_fee_sum_not_charged", "error",
        ],
    )

    clients = {
        ip: TraceClient(
            ip, args.server_host, args.udp_port, args.tcp_port,
            timeout=args.socket_timeout,
        )
        for ip in evaluation_source_ips
    }
    pacer = TracePacer(args.speedup)
    retry_heap: list[tuple[float, int, str]] = []
    active: dict[str, FlowRuntime] = {}
    good_terminal_results: list[dict[str, Any]] = []
    heap_order = 0

    # Online aggregate accounting replaces server_jobs.csv + evaluate.py merge.
    algorithm_cost_a = 0.0
    adversary_cost_b = 0.0
    serviced_flows = 0
    good_flows_serviced = 0
    bad_flows_serviced = 0
    failed_or_timed_out_flows = 0
    serviced_protocol_counts: dict[str, int] = {}
    serviced_destination_ips: set[str] = set()

    good_required_price_sum = 0.0
    bad_required_price_sum = 0.0
    good_accepted_fee_sum = 0.0
    bad_submitted_fee_sum = 0.0
    good_submitted_fee_sum = 0.0
    total_good_charged_fee = 0.0
    total_bad_charged_fee = 0.0
    total_good_rejected_fee_not_charged = 0.0
    total_request_attempts_for_serviced_flows = 0
    total_rejections_for_serviced_flows = 0
    good_rejections_for_serviced_flows = 0
    bad_rejections_for_serviced_flows = 0
    total_overpayment_on_serviced_attempts = 0.0

    pearson = RunningPearson()
    plot_points: list[dict[str, Any]] = []
    last_curve_point: dict[str, Any] | None = None
    last_saved_plot_job = 0

    eval_iterator = enumerate(
        iter_evaluation_rows(
            row_factory,
            max_evaluation_flows=args.max_evaluation_flows,
        ),
        start=1,
    )
    try:
        next_initial = next(eval_iterator, None)

        def account_completed_flow(runtime: FlowRuntime, ack: dict[str, Any]) -> None:
            nonlocal algorithm_cost_a, adversary_cost_b
            nonlocal serviced_flows, good_flows_serviced, bad_flows_serviced
            nonlocal good_required_price_sum, bad_required_price_sum
            nonlocal good_accepted_fee_sum, bad_submitted_fee_sum, good_submitted_fee_sum
            nonlocal total_good_charged_fee, total_bad_charged_fee
            nonlocal total_good_rejected_fee_not_charged
            nonlocal total_request_attempts_for_serviced_flows
            nonlocal total_rejections_for_serviced_flows
            nonlocal good_rejections_for_serviced_flows, bad_rejections_for_serviced_flows
            nonlocal total_overpayment_on_serviced_attempts
            nonlocal last_curve_point, last_saved_plot_job

            required_price = float(ack.get("price", runtime.last_required_price or 0.0))
            accepted_fee = float(ack.get("accepted_fee", ack.get("attached_fee", 0.0)))
            submitted_fee_sum = float(ack.get("submitted_fee_sum", runtime.submitted_fee_sum))
            attempt_count = int(ack.get("attempt_count", runtime.attempt_count))
            rejection_count = int(ack.get("rejection_count", runtime.rejection_count))
            overpayment = float(ack.get("overpayment", 0.0))

            charged_fee = accepted_fee if runtime.is_good else submitted_fee_sum
            algorithm_cost_a += 1.0 + (charged_fee if runtime.is_good else 0.0)
            adversary_cost_b += charged_fee if not runtime.is_good else 0.0
            serviced_flows += 1

            protocol_key = str(int(runtime.row["Protocol"]))
            serviced_protocol_counts[protocol_key] = serviced_protocol_counts.get(protocol_key, 0) + 1
            serviced_destination_ips.add(str(runtime.row["Destination IP"]))

            total_request_attempts_for_serviced_flows += attempt_count
            total_rejections_for_serviced_flows += rejection_count
            total_overpayment_on_serviced_attempts += overpayment

            if runtime.is_good:
                good_flows_serviced += 1
                good_required_price_sum += required_price
                good_accepted_fee_sum += accepted_fee
                good_submitted_fee_sum += submitted_fee_sum
                total_good_charged_fee += charged_fee
                total_good_rejected_fee_not_charged += submitted_fee_sum - accepted_fee
                good_rejections_for_serviced_flows += rejection_count
            else:
                bad_flows_serviced += 1
                bad_required_price_sum += required_price
                bad_submitted_fee_sum += submitted_fee_sum
                total_bad_charged_fee += charged_fee
                bad_rejections_for_serviced_flows += rejection_count

            empirical = adversary_cost_b / algorithm_cost_a if algorithm_cost_a > 0 else math.nan
            theoretical = theorem_proxy_value(
                algorithm=args.algorithm,
                adversary_cost_b=adversary_cost_b,
                cumulative_good_jobs=good_flows_serviced,
                theorem2_m=theorem2_m,
            )
            pearson.update(empirical, theoretical)
            last_curve_point = {
                "cumulative_jobs": serviced_flows,
                "cumulative_good_jobs": good_flows_serviced,
                "algorithm_cost": algorithm_cost_a,
                "adversary_cost": adversary_cost_b,
                "B_over_A": empirical,
                "theorem_proxy_raw": theoretical,
            }
            if serviced_flows % PLOT_EVERY == 0:
                plot_points.append(dict(last_curve_point))
                last_saved_plot_job = serviced_flows

        def finalize(
            runtime: FlowRuntime,
            *,
            status: str,
            event_time: float | None,
            ack: dict[str, Any] | None = None,
            error: str = "",
            timed_out: bool = False,
        ) -> None:
            nonlocal failed_or_timed_out_flows
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
            accepted_fee = (
                float(ack.get("accepted_fee", ack.get("attached_fee", 0.0)))
                if completed else 0.0
            )
            submitted_fee_sum = float(ack.get("submitted_fee_sum", runtime.submitted_fee_sum))
            deadline = runtime.deadline_trace_time
            completed_before_timeout = bool(
                completed and (deadline is None or (event_time is not None and event_time <= deadline))
            )
            result = {
                "sequence_id": runtime.sequence_id,
                "flow_uid": runtime.row["flow_uid"],
                "source_ip": runtime.row["Source IP"],
                "destination_ip": runtime.row["Destination IP"],
                "protocol": int(runtime.row["Protocol"]),
                "status": status,
                "price": ack.get("price", runtime.last_required_price or ""),
                "accepted_fee": accepted_fee if completed else "",
                "submitted_fee_sum": submitted_fee_sum,
                "attempt_count": int(ack.get("attempt_count", runtime.attempt_count)),
                "rejection_count": int(ack.get("rejection_count", runtime.rejection_count)),
                "generated_trace_time": runtime.generated_trace_time,
                "completion_trace_time": completion_trace_time,
                "completion_latency_trace_seconds": completion_latency_trace,
                "completion_latency_wall_seconds": terminal_wall_latency if completed else "",
                "deadline_trace_time": deadline if deadline is not None else "",
                "completed_before_timeout": completed_before_timeout,
                "timed_out": timed_out,
                "error": error,
            }
            runtime.result = result

            if completed:
                account_completed_flow(runtime, ack)
            else:
                failed_or_timed_out_flows += 1
                try:
                    control_client.request(
                        {"command": "abandon_flow", "flow_uid": str(runtime.row["flow_uid"])}
                    )
                except Exception:
                    pass

            if runtime.is_good:
                good_terminal_results.append(result)
            active.pop(str(runtime.row["flow_uid"]), None)

        def observe_initial(runtime: FlowRuntime) -> None:
            if args.mode != "sliding":
                return
            estimator_response = estimator_client.request(
                {
                    "command": "observe",
                    "trace_time": runtime.generated_trace_time,
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
                _, next_row = next_initial
                next_initial_time = float(next_row["parsed_timestamp"].timestamp())
            next_retry_time = retry_heap[0][0] if retry_heap else math.inf

            # New original arrivals win exact-time ties over retries.
            is_initial_event = next_initial_time <= next_retry_time
            if is_initial_event:
                sequence_id, row = next_initial
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

            if (
                runtime.is_good
                and runtime.deadline_trace_time is not None
                and event_time > runtime.deadline_trace_time
            ):
                finalize(
                    runtime,
                    status="timed_out",
                    event_time=None,
                    error="good-flow logical deadline expired before the next attempt",
                    timed_out=True,
                )
                if is_initial_event:
                    observe_initial(runtime)
                continue

            client = clients[str(runtime.row["Source IP"])]
            ack: dict[str, Any] = {}
            try:
                if args.algorithm == "linear":
                    message = attempt_message(runtime, event_time, fee=None)
                    runtime.attempt_count = 1
                    ack = client.send_once(message)
                    runtime.last_required_price = (
                        float(ack.get("price", 0.0)) if ack.get("ok") else None
                    )
                    runtime.last_attached_fee = (
                        float(ack.get("attached_fee", ack.get("price", 0.0)))
                        if ack.get("ok") else None
                    )
                    runtime.submitted_fee_sum = float(
                        ack.get("submitted_fee_sum", runtime.last_attached_fee or 0.0)
                    )
                else:
                    if runtime.is_good:
                        fee = runtime.current_fee
                    else:
                        quote = control_client.request(
                            {"command": "quote", "trace_time": event_time}
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
                    runtime.rejection_count = int(
                        ack.get("rejection_count", runtime.rejection_count + 1)
                    )
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
                        if (
                            runtime.deadline_trace_time is not None
                            and next_retry > runtime.deadline_trace_time
                        ):
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

        # The good-flow file is small relative to the full trace; retain it for
        # completion/retry analysis and preserve original-flow sequence order.
        for result in sorted(good_terminal_results, key=lambda item: int(item["sequence_id"])):
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
        good_metrics_log.close()

    # Always include the final serviced flow as a plot checkpoint.
    if last_curve_point is not None and serviced_flows != last_saved_plot_job:
        plot_points.append(dict(last_curve_point))

    plot_columns = [
        "cumulative_jobs", "cumulative_good_jobs", "algorithm_cost",
        "adversary_cost", "B_over_A", "theorem_proxy_raw",
    ]
    pd.DataFrame(plot_points, columns=plot_columns).to_csv(
        output_dir / "B_over_A_plot_points.csv", index=False
    )

    pearson_value = pearson.value()
    theorem_metadata = {
        "algorithm": args.algorithm,
        "theorem": "Theorem 2" if args.algorithm == "linear-power" else "Theorem 1",
        "theoretical_curve": "raw/unscaled proxy",
        "scaling": "none",
        "normalization": "none",
        "rmse": "not calculated",
        "pearson_correlation": pearson_value,
        "pearson_basis": "all valid serviced-flow points from raw empirical B/A and raw theoretical proxy",
        "pearson_flow_points": pearson.n,
        "pearson_computation": "online numerically stable running covariance; no flow-point sampling",
        "plot_checkpoint_interval_flows": PLOT_EVERY,
        "plot_checkpoints": int(len(plot_points)),
        "theorem2_M": theorem2_m if args.algorithm == "linear-power" else None,
        "theorem2_M_source": theorem2_m_source if args.algorithm == "linear-power" else None,
        "theorem2_delta_proxy_trace_seconds": (
            theorem2_delta_proxy if args.algorithm == "linear-power" else None
        ),
        "theorem2_delta_proxy_definition": (
            "configured retry delay; experimental proxy for theoretical Delta"
            if args.algorithm == "linear-power" else None
        ),
        "dataset": Path(args.csv).name,
        "mode": args.mode,
    }
    (output_dir / "theorem_proxy_metadata.json").write_text(
        json.dumps(theorem_metadata, indent=2), encoding="utf-8"
    )

    good_completed = [r for r in good_terminal_results if r["status"] == "ok"]
    good_timed_out = [r for r in good_terminal_results if bool(r["timed_out"])]
    trace_latencies = [
        float(r["completion_latency_trace_seconds"])
        for r in good_completed if r["completion_latency_trace_seconds"] != ""
    ]
    wall_latencies = [
        float(r["completion_latency_wall_seconds"])
        for r in good_completed if r["completion_latency_wall_seconds"] != ""
    ]
    good_rejections = [int(r["rejection_count"]) for r in good_terminal_results]
    good_final_fees = [
        float(r["accepted_fee"]) for r in good_completed if r["accepted_fee"] != ""
    ]

    evaluation_summary = {
        "algorithm": args.algorithm,
        "good_fee_policy": "accepted fee only; rejected good-flow fees are not charged",
        "bad_fee_policy": "all submitted bad-flow fees are charged",
        "serviced_flows": serviced_flows,
        "good_flows_serviced": good_flows_serviced,
        "bad_flows_serviced": bad_flows_serviced,
        "evaluation_flows": int(analysis.evaluation_flow_count),
        "failed_or_timed_out_flows": failed_or_timed_out_flows,
        "evaluation_unique_protocol_count": int(len(evaluation_protocol_counts)),
        "evaluation_protocol_counts": evaluation_protocol_counts,
        "serviced_protocol_counts": serviced_protocol_counts,
        "pricing_scope": "global",
        "pricing_states": 1 if serviced_flows > 0 else 0,
        "destination_ips_observed": len(serviced_destination_ips),
        "final_algorithm_cost_A": float(algorithm_cost_a),
        "final_adversary_cost_B": float(adversary_cost_b),
        "final_B_over_A": (
            float(adversary_cost_b / algorithm_cost_a) if algorithm_cost_a > 0 else 0.0
        ),
        "mean_good_required_price": safe_mean_total(good_required_price_sum, good_flows_serviced),
        "mean_bad_required_price": safe_mean_total(bad_required_price_sum, bad_flows_serviced),
        "mean_good_accepted_fee": safe_mean_total(good_accepted_fee_sum, good_flows_serviced),
        "mean_bad_submitted_fee_sum": safe_mean_total(bad_submitted_fee_sum, bad_flows_serviced),
        "total_good_charged_fee": float(total_good_charged_fee),
        "total_bad_charged_fee": float(total_bad_charged_fee),
        "total_good_submitted_fee_sum": float(good_submitted_fee_sum),
        "total_good_rejected_fee_not_charged": float(total_good_rejected_fee_not_charged),
        "total_request_attempts_for_serviced_flows": int(total_request_attempts_for_serviced_flows),
        "total_rejections_for_serviced_flows": int(total_rejections_for_serviced_flows),
        "good_rejections_for_serviced_flows": int(good_rejections_for_serviced_flows),
        "bad_rejections_for_serviced_flows": int(bad_rejections_for_serviced_flows),
        "total_overpayment_on_serviced_attempts": float(total_overpayment_on_serviced_attempts),
        "good_flows_observed": len(good_terminal_results),
        "good_flows_completed": len(good_completed),
        "good_flows_timed_out": len(good_timed_out),
        "good_completion_rate": (
            float(len(good_completed) / len(good_terminal_results))
            if good_terminal_results else None
        ),
        "mean_good_completion_latency_trace_seconds": safe_mean_total(
            sum(trace_latencies), len(trace_latencies)
        ),
        "p95_good_completion_latency_trace_seconds": percentile_95(trace_latencies),
        "mean_good_completion_latency_wall_seconds": safe_mean_total(
            sum(wall_latencies), len(wall_latencies)
        ),
        "mean_good_rejections": safe_mean_total(sum(good_rejections), len(good_rejections)),
        "max_good_rejections": max(good_rejections) if good_rejections else 0,
        "mean_good_final_accepted_fee": safe_mean_total(sum(good_final_fees), len(good_final_fees)),
        "pearson_correlation": pearson_value,
        "pearson_flow_points": pearson.n,
        "plot_checkpoint_interval_flows": PLOT_EVERY,
        "plot_checkpoints": len(plot_points),
        "large_intermediate_csvs_written": False,
    }
    (output_dir / "evaluation_summary.json").write_text(
        json.dumps(evaluation_summary, indent=2), encoding="utf-8"
    )

    # Add the estimator result only to the small JSON metadata, never a full-flow file.
    selection_summary["initial_estimate"] = initial_estimate
    selection_summary["evaluation_good_flow_count"] = evaluation_good_count
    (output_dir / "selection_summary.json").write_text(
        json.dumps(selection_summary, indent=2), encoding="utf-8"
    )

    print(json.dumps(evaluation_summary, indent=2))
    print(json.dumps({"ok": True, **selection_summary}, indent=2))


if __name__ == "__main__":
    main()
