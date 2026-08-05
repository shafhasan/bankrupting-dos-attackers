#!/usr/bin/env python3
"""
LINEAR pricing with the paper's Weibull rate estimator.

Experiment implemented by this script
-------------------------------------
1. Read a CIC-DDoS2019 flow CSV and sort all rows by timestamp.
2. Use every BENIGN-labelled flow only for offline Weibull fitting.
3. Fit a two-parameter Weibull distribution to positive benign inter-arrival
   times, with location fixed at zero.
4. Compute the estimator rate

       rho = 1 / E[X]
       g_hat(I) = rho * length(I)

   where X is a benign inter-arrival time.
5. Use estimator-defined fixed-duration iterations of length E[X].
6. Replay ALL flows through LINEAR. Pricing does not inspect the label:

       PRICE = s + 1

   where s is the number of already-serviced jobs in that iteration.
7. Assume every flow pays exactly PRICE and is serviced.
8. Use labels only after pricing to calculate:

       B = fees paid by malicious flows
       A = fees paid by benign flows + server service cost

   with normalized server service cost 1 per serviced flow.
9. Save cumulative results after 100, 200, 300, ... total jobs.
10. Compare experimental B/A with a scaled constant-gamma Theorem 1 proxy:

       B / (sqrt(B * (g + 1)) + (g + 1))

    The single scaling constant is selected at the final valid checkpoint.

This is an offline full-trace estimator baseline because the complete trace's
BENIGN labels are used to fit the Weibull model before the replay begins.
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats


DEFAULT_BENIGN_LABEL = "BENIGN"
DEFAULT_TIMESTAMP_COLUMN = "Timestamp"
DEFAULT_LABEL_COLUMN = "Label"
DEFAULT_CHECKPOINT_SIZE = 100
DEFAULT_CHUNK_THRESHOLD_MB = 3078.0
DEFAULT_CHUNK_SIZE = 250_000


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fit a Weibull model to benign flow inter-arrival times, use it "
            "to define LINEAR iterations, and compare cumulative B/A with "
            "a scaled Theorem 1 trend by job count."
        )
    )
    parser.add_argument(
        "csv_file",
        help="Path to one CIC-DDoS2019 flow CSV file.",
    )
    parser.add_argument(
        "--output-dir",
        default="linear_weibull_results",
        help="Directory for CSV, JSON, and PNG outputs.",
    )
    parser.add_argument(
        "--benign-label",
        default=DEFAULT_BENIGN_LABEL,
        help="Label value treated as a good/benign flow.",
    )
    parser.add_argument(
        "--timestamp-column",
        default=DEFAULT_TIMESTAMP_COLUMN,
        help="Timestamp column name after surrounding whitespace is removed.",
    )
    parser.add_argument(
        "--label-column",
        default=DEFAULT_LABEL_COLUMN,
        help="Label column name after surrounding whitespace is removed.",
    )
    parser.add_argument(
        "--checkpoint-size",
        type=int,
        default=DEFAULT_CHECKPOINT_SIZE,
        help="Record cumulative costs after every N total jobs.",
    )
    parser.add_argument(
        "--save-flow-trace",
        action="store_true",
        help=(
            "Save one row per flow with iteration, price, and cumulative costs. "
            "This may create a large CSV."
        ),
    )
    parser.add_argument(
        "--read-mode",
        choices=("auto", "full", "chunked"),
        default="auto",
        help=(
            "CSV loading strategy. 'auto' uses the file-size threshold, "
            "'full' always reads the complete file at once, and 'chunked' "
            "always uses bounded-size chunks."
        ),
    )

    parser.add_argument(
        "--chunk-threshold-mb",
        type=float,
        default=DEFAULT_CHUNK_THRESHOLD_MB,
        help=(
            "In auto mode, use chunked loading when the CSV is at least "
            "this large in MiB. Default: %(default)s MiB."
        ),
    )

    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help=(
            "Rows per chunk when chunked loading is used. "
            "Default: %(default)s."
        ),
    )
    # parser.add_argument(
    #     "--linear-y-axis",
    #     action="store_true",
    #     help=(
    #         "Use a linear y-axis for the B/A plot. By default, the script "
    #         "automatically uses log scale if the positive ratio range is large."
    #     ),
    # )
    return parser.parse_args()


def resolve_raw_column_names(
    csv_path: Path,
    timestamp_column: str,
    label_column: str,
) -> tuple[str, str]:
    """Resolve columns even when the CSV header contains leading spaces."""
    header = pd.read_csv(csv_path, nrows=0)
    normalized_to_raw: dict[str, str] = {}

    for raw_name in header.columns:
        normalized = str(raw_name).strip()
        if normalized in normalized_to_raw:
            raise ValueError(
                f"Multiple columns become {normalized!r} after stripping whitespace."
            )
        normalized_to_raw[normalized] = str(raw_name)

    missing = [
        name
        for name in (timestamp_column, label_column)
        if name not in normalized_to_raw
    ]
    if missing:
        available = sorted(normalized_to_raw)
        raise KeyError(
            f"Missing required column(s): {missing}. "
            f"Available normalized columns include: {available}"
        )

    return (
        normalized_to_raw[timestamp_column],
        normalized_to_raw[label_column],
    )


def parse_timestamp_series(values: pd.Series) -> pd.Series:
    """Parse CIC timestamps on both old and new pandas versions.

    pandas 2.x understands ``format="mixed"``. Some older pandas versions do
    not raise an exception for that argument when ``errors="coerce"`` is
    used; instead, they silently convert every value to NaT. Therefore, first
    use the ordinary parser, which handles the standard CIC-DDoS2019 format
    (for example, ``2018-12-01 13:04:45.928673``), and only then try the
    pandas-2.x mixed-format parser on values that remain invalid.
    """
    cleaned = values.astype("string").str.strip()

    # Works with the standard CIC-DDoS2019 timestamp format and with old
    # pandas releases.
    parsed = pd.to_datetime(cleaned, errors="coerce")

    remaining = parsed.isna() & cleaned.notna() & cleaned.ne("")
    if remaining.any():
        try:
            mixed = pd.to_datetime(
                cleaned.loc[remaining],
                errors="coerce",
                format="mixed",
            )
        except (TypeError, ValueError):
            mixed = pd.to_datetime(
                cleaned.loc[remaining],
                errors="coerce",
            )
        parsed.loc[remaining] = mixed

    return parsed


def _validate_loaded_flows(
    flows: pd.DataFrame,
    validation: dict[str, int],
    csv_path: Path,
    raw_timestamp: str,
) -> None:
    """Check that the loaded trace is usable for the experiment."""
    if len(flows) == 0:
        raw_examples = (
            pd.read_csv(
                csv_path,
                usecols=[raw_timestamp],
                nrows=8,
            )[raw_timestamp]
            .astype(str)
            .tolist()
        )

        raise ValueError(
            "No rows with valid timestamps remain. "
            f"pandas version: {pd.__version__}. "
            f"First raw timestamp values: {raw_examples}"
        )

    if validation["good_flows"] < 3:
        raise ValueError(
            "At least three benign flows are required "
            "for Weibull fitting."
        )


def _finalize_flow_order(
    flows: pd.DataFrame,
) -> pd.DataFrame:
    """
    Preserve the existing order when timestamps are already chronological.
    Otherwise, perform the same stable timestamp/original-row sort used by
    the original script.
    """
    if flows["timestamp"].is_monotonic_increasing:
        print("Input is already chronological; sorting skipped.")
        return flows.reset_index(drop=True)

    print("Input is not chronological; performing stable sort.")

    return (
        flows.sort_values(
            ["timestamp", "original_row"],
            kind="mergesort",
        )
        .reset_index(drop=True)
    )


def load_and_sort_flows_full(
    csv_path: Path,
    timestamp_column: str,
    label_column: str,
    benign_label: str,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """
    Original full-file loading method.

    This is used for CSV files below the configured size threshold.
    """
    raw_timestamp, raw_label = resolve_raw_column_names(
        csv_path,
        timestamp_column,
        label_column,
    )

    flows = pd.read_csv(
        csv_path,
        usecols=[raw_timestamp, raw_label],
        low_memory=False,
    ).rename(
        columns={
            raw_timestamp: "timestamp_raw",
            raw_label: "label",
        }
    )

    original_rows = len(flows)

    flows["original_row"] = np.arange(
        original_rows,
        dtype=np.int64,
    )

    flows["timestamp"] = parse_timestamp_series(
        flows["timestamp_raw"]
    )

    flows["label"] = (
        flows["label"]
        .astype("string")
        .str.strip()
    )

    invalid_timestamp_count = int(
        flows["timestamp"].isna().sum()
    )

    missing_label_count = int(
        flows["label"].isna().sum()
    )

    flows = flows.dropna(
        subset=["timestamp"]
    ).copy()

    flows["label"] = flows["label"].fillna(
        "<MISSING>"
    )

    # The original text timestamp is no longer needed.
    flows.drop(
        columns=["timestamp_raw"],
        inplace=True,
    )

    benign_key = benign_label.strip().casefold()

    flows["is_good"] = (
        flows["label"]
        .astype(str)
        .str.strip()
        .str.casefold()
        .eq(benign_key)
    )

    flows = _finalize_flow_order(flows)

    validation = {
        "input_rows": int(original_rows),
        "rows_used": int(len(flows)),
        "invalid_timestamp_rows_removed": (
            invalid_timestamp_count
        ),
        "missing_label_rows": missing_label_count,
        "good_flows": int(flows["is_good"].sum()),
        "bad_flows": int((~flows["is_good"]).sum()),
    }

    _validate_loaded_flows(
        flows=flows,
        validation=validation,
        csv_path=csv_path,
        raw_timestamp=raw_timestamp,
    )

    return flows, validation


def load_and_sort_flows_chunked(
    csv_path: Path,
    timestamp_column: str,
    label_column: str,
    benign_label: str,
    chunk_size: int,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """
    Read a large CSV in chunks.

    This changes only how the CSV is loaded. Timestamps, labels, sorting,
    iteration assignment, LINEAR prices, and cost calculations remain the
    same as in full-file mode.
    """
    if chunk_size <= 0:
        raise ValueError(
            "--chunk-size must be a positive integer."
        )

    raw_timestamp, raw_label = resolve_raw_column_names(
        csv_path,
        timestamp_column,
        label_column,
    )

    benign_key = benign_label.strip().casefold()

    flow_parts: list[pd.DataFrame] = []

    input_rows = 0
    invalid_timestamp_count = 0
    missing_label_count = 0

    reader = pd.read_csv(
        csv_path,
        usecols=[raw_timestamp, raw_label],
        chunksize=chunk_size,
        low_memory=True,
    )

    for chunk_number, chunk in enumerate(
        reader,
        start=1,
    ):
        rows_in_chunk = len(chunk)

        chunk.rename(
            columns={
                raw_timestamp: "timestamp_raw",
                raw_label: "label",
            },
            inplace=True,
        )

        chunk["original_row"] = np.arange(
            input_rows,
            input_rows + rows_in_chunk,
            dtype=np.int64,
        )

        input_rows += rows_in_chunk

        chunk["timestamp"] = parse_timestamp_series(
            chunk["timestamp_raw"]
        )

        chunk["label"] = (
            chunk["label"]
            .astype("string")
            .str.strip()
        )

        invalid_timestamp_count += int(
            chunk["timestamp"].isna().sum()
        )

        missing_label_count += int(
            chunk["label"].isna().sum()
        )

        chunk = chunk.dropna(
            subset=["timestamp"]
        ).copy()

        if chunk.empty:
            print(
                f"Chunk {chunk_number:,}: "
                "no valid timestamp rows."
            )
            continue

        chunk["label"] = chunk["label"].fillna(
            "<MISSING>"
        )

        chunk["is_good"] = (
            chunk["label"]
            .astype(str)
            .str.strip()
            .str.casefold()
            .eq(benign_key)
        )

        # Keep only the fields required by the remainder of the script.
        chunk = chunk[
            [
                "timestamp",
                "label",
                "original_row",
                "is_good",
            ]
        ]

        flow_parts.append(chunk)

        print(
            f"Read chunk {chunk_number:,}: "
            f"{input_rows:,} input rows processed."
        )

    if not flow_parts:
        empty_flows = pd.DataFrame(
            columns=[
                "timestamp",
                "label",
                "original_row",
                "is_good",
            ]
        )

        validation = {
            "input_rows": int(input_rows),
            "rows_used": 0,
            "invalid_timestamp_rows_removed": int(
                invalid_timestamp_count
            ),
            "missing_label_rows": int(
                missing_label_count
            ),
            "good_flows": 0,
            "bad_flows": 0,
        }

        _validate_loaded_flows(
            flows=empty_flows,
            validation=validation,
            csv_path=csv_path,
            raw_timestamp=raw_timestamp,
        )

    flows = pd.concat(
        flow_parts,
        ignore_index=True,
        copy=False,
    )

    del flow_parts

    flows = _finalize_flow_order(flows)

    validation = {
        "input_rows": int(input_rows),
        "rows_used": int(len(flows)),
        "invalid_timestamp_rows_removed": int(
            invalid_timestamp_count
        ),
        "missing_label_rows": int(
            missing_label_count
        ),
        "good_flows": int(flows["is_good"].sum()),
        "bad_flows": int((~flows["is_good"]).sum()),
    }

    _validate_loaded_flows(
        flows=flows,
        validation=validation,
        csv_path=csv_path,
        raw_timestamp=raw_timestamp,
    )

    return flows, validation


def _is_out_of_memory_error(
    exception: BaseException,
) -> bool:
    """Recognize pandas/Python memory failures."""
    if isinstance(exception, MemoryError):
        return True

    message = str(exception).casefold()

    return (
        "out of memory" in message
        or "unable to allocate" in message
        or "memoryerror" in message
    )


def _validate_loaded_flows(
    flows: pd.DataFrame,
    validation: dict[str, int],
    csv_path: Path,
    raw_timestamp: str,
) -> None:
    """Check that the loaded trace is usable for the experiment."""
    if len(flows) == 0:
        raw_examples = (
            pd.read_csv(
                csv_path,
                usecols=[raw_timestamp],
                nrows=8,
            )[raw_timestamp]
            .astype(str)
            .tolist()
        )

        raise ValueError(
            "No rows with valid timestamps remain. "
            f"pandas version: {pd.__version__}. "
            f"First raw timestamp values: {raw_examples}"
        )

    if validation["good_flows"] < 3:
        raise ValueError(
            "At least three benign flows are required "
            "for Weibull fitting."
        )


def _finalize_flow_order(
    flows: pd.DataFrame,
) -> pd.DataFrame:
    """
    Preserve the existing order when timestamps are already chronological.
    Otherwise, perform the same stable timestamp/original-row sort used by
    the original script.
    """
    if flows["timestamp"].is_monotonic_increasing:
        print("Input is already chronological; sorting skipped.")
        return flows.reset_index(drop=True)

    print("Input is not chronological; performing stable sort.")

    return (
        flows.sort_values(
            ["timestamp", "original_row"],
            kind="mergesort",
        )
        .reset_index(drop=True)
    )


def load_and_sort_flows_full(
    csv_path: Path,
    timestamp_column: str,
    label_column: str,
    benign_label: str,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """
    Original full-file loading method.

    This is used for CSV files below the configured size threshold.
    """
    raw_timestamp, raw_label = resolve_raw_column_names(
        csv_path,
        timestamp_column,
        label_column,
    )

    flows = pd.read_csv(
        csv_path,
        usecols=[raw_timestamp, raw_label],
        low_memory=False,
    ).rename(
        columns={
            raw_timestamp: "timestamp_raw",
            raw_label: "label",
        }
    )

    original_rows = len(flows)

    flows["original_row"] = np.arange(
        original_rows,
        dtype=np.int64,
    )

    flows["timestamp"] = parse_timestamp_series(
        flows["timestamp_raw"]
    )

    flows["label"] = (
        flows["label"]
        .astype("string")
        .str.strip()
    )

    invalid_timestamp_count = int(
        flows["timestamp"].isna().sum()
    )

    missing_label_count = int(
        flows["label"].isna().sum()
    )

    flows = flows.dropna(
        subset=["timestamp"]
    ).copy()

    flows["label"] = flows["label"].fillna(
        "<MISSING>"
    )

    # The original text timestamp is no longer needed.
    flows.drop(
        columns=["timestamp_raw"],
        inplace=True,
    )

    benign_key = benign_label.strip().casefold()

    flows["is_good"] = (
        flows["label"]
        .astype(str)
        .str.strip()
        .str.casefold()
        .eq(benign_key)
    )

    flows = _finalize_flow_order(flows)

    validation = {
        "input_rows": int(original_rows),
        "rows_used": int(len(flows)),
        "invalid_timestamp_rows_removed": (
            invalid_timestamp_count
        ),
        "missing_label_rows": missing_label_count,
        "good_flows": int(flows["is_good"].sum()),
        "bad_flows": int((~flows["is_good"]).sum()),
    }

    _validate_loaded_flows(
        flows=flows,
        validation=validation,
        csv_path=csv_path,
        raw_timestamp=raw_timestamp,
    )

    return flows, validation


def load_and_sort_flows_chunked(
    csv_path: Path,
    timestamp_column: str,
    label_column: str,
    benign_label: str,
    chunk_size: int,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """
    Read a large CSV in chunks.

    This changes only how the CSV is loaded. Timestamps, labels, sorting,
    iteration assignment, LINEAR prices, and cost calculations remain the
    same as in full-file mode.
    """
    if chunk_size <= 0:
        raise ValueError(
            "--chunk-size must be a positive integer."
        )

    raw_timestamp, raw_label = resolve_raw_column_names(
        csv_path,
        timestamp_column,
        label_column,
    )

    benign_key = benign_label.strip().casefold()

    flow_parts: list[pd.DataFrame] = []

    input_rows = 0
    invalid_timestamp_count = 0
    missing_label_count = 0

    reader = pd.read_csv(
        csv_path,
        usecols=[raw_timestamp, raw_label],
        chunksize=chunk_size,
        low_memory=True,
    )

    for chunk_number, chunk in enumerate(
        reader,
        start=1,
    ):
        rows_in_chunk = len(chunk)

        chunk.rename(
            columns={
                raw_timestamp: "timestamp_raw",
                raw_label: "label",
            },
            inplace=True,
        )

        chunk["original_row"] = np.arange(
            input_rows,
            input_rows + rows_in_chunk,
            dtype=np.int64,
        )

        input_rows += rows_in_chunk

        chunk["timestamp"] = parse_timestamp_series(
            chunk["timestamp_raw"]
        )

        chunk["label"] = (
            chunk["label"]
            .astype("string")
            .str.strip()
        )

        invalid_timestamp_count += int(
            chunk["timestamp"].isna().sum()
        )

        missing_label_count += int(
            chunk["label"].isna().sum()
        )

        chunk = chunk.dropna(
            subset=["timestamp"]
        ).copy()

        if chunk.empty:
            print(
                f"Chunk {chunk_number:,}: "
                "no valid timestamp rows."
            )
            continue

        chunk["label"] = chunk["label"].fillna(
            "<MISSING>"
        )

        chunk["is_good"] = (
            chunk["label"]
            .astype(str)
            .str.strip()
            .str.casefold()
            .eq(benign_key)
        )

        # Keep only the fields required by the remainder of the script.
        chunk = chunk[
            [
                "timestamp",
                "label",
                "original_row",
                "is_good",
            ]
        ]

        flow_parts.append(chunk)

        print(
            f"Read chunk {chunk_number:,}: "
            f"{input_rows:,} input rows processed."
        )

    if not flow_parts:
        empty_flows = pd.DataFrame(
            columns=[
                "timestamp",
                "label",
                "original_row",
                "is_good",
            ]
        )

        validation = {
            "input_rows": int(input_rows),
            "rows_used": 0,
            "invalid_timestamp_rows_removed": int(
                invalid_timestamp_count
            ),
            "missing_label_rows": int(
                missing_label_count
            ),
            "good_flows": 0,
            "bad_flows": 0,
        }

        _validate_loaded_flows(
            flows=empty_flows,
            validation=validation,
            csv_path=csv_path,
            raw_timestamp=raw_timestamp,
        )

    flows = pd.concat(
        flow_parts,
        ignore_index=True,
        copy=False,
    )

    del flow_parts

    flows = _finalize_flow_order(flows)

    validation = {
        "input_rows": int(input_rows),
        "rows_used": int(len(flows)),
        "invalid_timestamp_rows_removed": int(
            invalid_timestamp_count
        ),
        "missing_label_rows": int(
            missing_label_count
        ),
        "good_flows": int(flows["is_good"].sum()),
        "bad_flows": int((~flows["is_good"]).sum()),
    }

    _validate_loaded_flows(
        flows=flows,
        validation=validation,
        csv_path=csv_path,
        raw_timestamp=raw_timestamp,
    )

    return flows, validation


def _is_out_of_memory_error(
    exception: BaseException,
) -> bool:
    """Recognize pandas/Python memory failures."""
    if isinstance(exception, MemoryError):
        return True

    message = str(exception).casefold()

    return (
        "out of memory" in message
        or "unable to allocate" in message
        or "memoryerror" in message
    )


def load_and_sort_flows(
    csv_path: Path,
    timestamp_column: str,
    label_column: str,
    benign_label: str,
    read_mode: str = "auto",
    chunk_threshold_mb: float = 1024.0,
    chunk_size: int = 250_000,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """
    Dynamically select full-file or chunked CSV loading.

    auto:
        Full loading below the size threshold.
        Chunked loading at or above the threshold.
        Retry with chunks if full loading unexpectedly runs out of memory.

    full:
        Always load the complete selected columns at once.

    chunked:
        Always use chunked loading.
    """
    if read_mode not in {
        "auto",
        "full",
        "chunked",
    }:
        raise ValueError(
            "--read-mode must be auto, full, or chunked."
        )

    if chunk_threshold_mb <= 0:
        raise ValueError(
            "--chunk-threshold-mb must be positive."
        )

    if chunk_size <= 0:
        raise ValueError(
            "--chunk-size must be positive."
        )

    file_size_mb = (
        csv_path.stat().st_size / (1024.0**2)
    )

    use_chunked = (
        read_mode == "chunked"
        or (
            read_mode == "auto"
            and file_size_mb >= chunk_threshold_mb
        )
    )

    if use_chunked:
        print(
            f"CSV size: {file_size_mb:,.1f} MiB. "
            f"Using chunked loading "
            f"({chunk_size:,} rows per chunk)."
        )

        return load_and_sort_flows_chunked(
            csv_path=csv_path,
            timestamp_column=timestamp_column,
            label_column=label_column,
            benign_label=benign_label,
            chunk_size=chunk_size,
        )

    print(
        f"CSV size: {file_size_mb:,.1f} MiB. "
        "Using full-file loading."
    )

    try:
        return load_and_sort_flows_full(
            csv_path=csv_path,
            timestamp_column=timestamp_column,
            label_column=label_column,
            benign_label=benign_label,
        )

    except (MemoryError, pd.errors.ParserError) as exc:
        if (
            read_mode == "full"
            or not _is_out_of_memory_error(exc)
        ):
            raise

        print(
            "Full-file loading ran out of memory. "
            "Retrying automatically with chunked loading."
        )

        return load_and_sort_flows_chunked(
            csv_path=csv_path,
            timestamp_column=timestamp_column,
            label_column=label_column,
            benign_label=benign_label,
            chunk_size=chunk_size,
        )


def calculate_benign_iats(flows: pd.DataFrame) -> tuple[np.ndarray, dict[str, int]]:
    good_times_ns = (
        flows.loc[flows["is_good"], "timestamp"]
        .astype("int64")
        .to_numpy(dtype=np.int64)
    )

    differences_seconds = np.diff(good_times_ns).astype(np.float64) / 1e9

    positive_mask = differences_seconds > 0.0
    positive_iats = differences_seconds[positive_mask]

    diagnostics = {
        "all_benign_interarrival_differences": int(differences_seconds.size),
        "positive_benign_interarrivals_used": int(positive_iats.size),
        "zero_benign_interarrivals_removed": int(
            np.count_nonzero(differences_seconds == 0.0)
        ),
        "negative_benign_interarrivals_removed": int(
            np.count_nonzero(differences_seconds < 0.0)
        ),
    }

    if positive_iats.size < 2:
        raise ValueError(
            "Too few positive benign inter-arrival times remain for Weibull fitting."
        )

    return positive_iats, diagnostics


def fit_weibull(iats_seconds: np.ndarray) -> dict[str, float | int]:
    """Fit Weibull(shape, scale) by MLE with location fixed at zero."""
    shape, location, scale = stats.weibull_min.fit(
        iats_seconds,
        floc=0.0,
    )

    shape = float(shape)
    location = float(location)
    scale = float(scale)

    if not (shape > 0.0 and scale > 0.0):
        raise FloatingPointError(
            f"Invalid fitted Weibull parameters: shape={shape}, scale={scale}."
        )

    fitted_mean = scale * math.gamma(1.0 + 1.0 / shape)
    rho = 1.0 / fitted_mean

    log_density = stats.weibull_min.logpdf(
        iats_seconds,
        shape,
        loc=location,
        scale=scale,
    )
    if not np.all(np.isfinite(log_density)):
        raise FloatingPointError("Non-finite Weibull log-likelihood values were produced.")

    log_likelihood = float(np.sum(log_density))
    # Shape and scale are estimated; location is fixed at zero.
    parameter_count = 2
    aic = 2 * parameter_count - 2 * log_likelihood

    # Approximate p-value: the same sample is used for fitting and testing.
    ks_result = stats.kstest(
        iats_seconds,
        "weibull_min",
        args=(shape, location, scale),
    )

    return {
        "sample_size": int(iats_seconds.size),
        "shape_k": shape,
        "location_fixed": location,
        "scale_lambda_seconds": scale,
        "empirical_mean_iat_seconds": float(np.mean(iats_seconds)),
        "empirical_median_iat_seconds": float(np.median(iats_seconds)),
        "weibull_mean_iat_seconds": float(fitted_mean),
        "rho_expected_good_flows_per_second": float(rho),
        "log_likelihood": log_likelihood,
        "AIC": float(aic),
        "KS_statistic": float(ks_result.statistic),
        "KS_p_value_approximate": float(ks_result.pvalue),
    }


def assign_linear_prices(
    flows: pd.DataFrame,
    iteration_length_seconds: float,
) -> pd.DataFrame:
    """
    Assign estimator iterations and LINEAR prices.

    Pricing deliberately does not use is_good or label. Every flow is assumed
    to pay the exact current price and be serviced.
    """
    if not np.isfinite(iteration_length_seconds) or iteration_length_seconds <= 0:
        raise ValueError("The fitted iteration length must be positive and finite.")

    timestamps_ns = flows["timestamp"].astype("int64").to_numpy(dtype=np.int64)
    first_timestamp_ns = int(timestamps_ns[0])
    elapsed_seconds = (timestamps_ns - first_timestamp_ns).astype(np.float64) / 1e9

    # Since g_hat(I) = rho * length(I), the reset threshold g_hat(I) >= 1
    # is reached after length(I) >= 1/rho = E[X]. Therefore, fixed bins of
    # width E[X] implement the Weibull estimator's iterations.
    iteration_id = np.floor(
        elapsed_seconds / iteration_length_seconds
    ).astype(np.int64)

    priced = flows.copy()
    priced["job_number"] = np.arange(1, len(priced) + 1, dtype=np.int64)
    priced["elapsed_seconds"] = elapsed_seconds
    priced["iteration_id"] = iteration_id

    # Within an iteration, the first job has s=0 and pays 1; the next pays 2.
    priced["position_in_iteration"] = (
        priced.groupby("iteration_id", sort=False).cumcount().to_numpy(dtype=np.int64)
        + 1
    )
    priced["price"] = priced["position_in_iteration"].astype(np.int64)

    prices = priced["price"].to_numpy(dtype=np.int64)
    is_good = priced["is_good"].to_numpy(dtype=bool)

    good_fee = np.where(is_good, prices, 0).astype(np.int64)
    bad_fee = np.where(~is_good, prices, 0).astype(np.int64)

    priced["good_fee"] = good_fee
    priced["adversary_fee"] = bad_fee
    priced["server_service_cost"] = np.int64(1)

    priced["cumulative_good_fees"] = np.cumsum(good_fee, dtype=np.int64)
    priced["cumulative_adversary_cost_B"] = np.cumsum(
        bad_fee,
        dtype=np.int64,
    )
    priced["cumulative_service_cost"] = priced["job_number"].astype(np.int64)
    priced["cumulative_algorithm_cost_A"] = (
        priced["cumulative_good_fees"] + priced["cumulative_service_cost"]
    )
    priced["cumulative_B_over_A"] = (
        priced["cumulative_adversary_cost_B"]
        / priced["cumulative_algorithm_cost_A"]
    )

    return priced


def create_checkpoints(
    trace: pd.DataFrame,
    checkpoint_size: int,
) -> pd.DataFrame:
    if checkpoint_size <= 0:
        raise ValueError("--checkpoint-size must be a positive integer.")

    number_of_jobs = len(trace)
    checkpoint_jobs = np.arange(
        checkpoint_size,
        number_of_jobs + 1,
        checkpoint_size,
        dtype=np.int64,
    )
    if checkpoint_jobs.size == 0 or checkpoint_jobs[-1] != number_of_jobs:
        checkpoint_jobs = np.append(checkpoint_jobs, number_of_jobs)

    checkpoint_indices = checkpoint_jobs - 1
    selected = trace.iloc[checkpoint_indices]

    checkpoints = pd.DataFrame(
        {
            "number_of_jobs": checkpoint_jobs,
            "benign_jobs": np.cumsum(
                trace["is_good"].to_numpy(dtype=np.int64),
                dtype=np.int64,
            )[checkpoint_indices],
            "malicious_jobs": np.cumsum(
                (~trace["is_good"]).to_numpy(dtype=np.int64),
                dtype=np.int64,
            )[checkpoint_indices],
            "honest_client_fees": selected["cumulative_good_fees"].to_numpy(),
            "adversary_cost_B": selected[
                "cumulative_adversary_cost_B"
            ].to_numpy(),
            "server_service_cost": selected[
                "cumulative_service_cost"
            ].to_numpy(),
            "algorithm_cost_A": selected[
                "cumulative_algorithm_cost_A"
            ].to_numpy(),
            "adversary_over_algorithm_B_over_A": selected[
                "cumulative_B_over_A"
            ].to_numpy(),
            "current_iteration_id": selected["iteration_id"].to_numpy(),
            "current_price": selected["price"].to_numpy(),
        }
    )
    return checkpoints


def add_theorem1_comparison(
    checkpoints: pd.DataFrame,
) -> tuple[pd.DataFrame, float]:
    """
    Add a constant-gamma Theorem 1 shape proxy to every checkpoint.

    Theorem 1 states that LINEAR has cost

        A = O(gamma^(5/2) * sqrt(B * (g + 1))
              + gamma^3 * (g + 1)).

    When gamma is treated as a fixed constant and hidden multiplicative
    constants are omitted, the corresponding adversary/algorithm ratio
    shape is

        B / (sqrt(B * (g + 1)) + (g + 1)).

    This is a shape comparison, not an exact prediction of B/A. A single
    global scaling constant is chosen so that the scaled theoretical proxy
    equals the experimental B/A value at the final valid checkpoint. The
    same constant is then applied to every checkpoint.
    """
    result = checkpoints.copy()

    adversary_cost = result["adversary_cost_B"].to_numpy(dtype=float)
    good_jobs = result["benign_jobs"].to_numpy(dtype=float)
    experimental_ratio = result[
        "adversary_over_algorithm_B_over_A"
    ].to_numpy(dtype=float)

    g_plus_one = good_jobs + 1.0
    denominator = np.sqrt(adversary_cost * g_plus_one) + g_plus_one

    theoretical_raw = np.full(len(result), np.nan, dtype=float)
    valid_raw = (
        np.isfinite(adversary_cost)
        & np.isfinite(g_plus_one)
        & np.isfinite(denominator)
        & (adversary_cost >= 0.0)
        & (g_plus_one > 0.0)
        & (denominator > 0.0)
    )
    theoretical_raw[valid_raw] = (
        adversary_cost[valid_raw] / denominator[valid_raw]
    )

    valid_scale = (
        valid_raw
        & np.isfinite(theoretical_raw)
        & (theoretical_raw > 0.0)
        & np.isfinite(experimental_ratio)
        & (experimental_ratio > 0.0)
    )
    valid_indices = np.flatnonzero(valid_scale)

    if valid_indices.size == 0:
        raise ValueError(
            "No valid checkpoint exists for scaling the Theorem 1 curve."
        )

    final_index = int(valid_indices[-1])
    scale_constant = float(
        experimental_ratio[final_index] / theoretical_raw[final_index]
    )
    theoretical_scaled = scale_constant * theoretical_raw

    result["theorem1_proxy_raw"] = theoretical_raw
    result["theorem1_scale_constant"] = scale_constant
    result["theorem1_proxy_scaled"] = theoretical_scaled

    return result, scale_constant


def create_iteration_summary(trace: pd.DataFrame) -> pd.DataFrame:
    grouped = trace.groupby("iteration_id", sort=False, observed=True)

    summary = grouped.agg(
        iteration_start=("timestamp", "min"),
        iteration_last_flow=("timestamp", "max"),
        total_jobs=("job_number", "size"),
        benign_jobs=("is_good", "sum"),
        maximum_price=("price", "max"),
        honest_client_fees=("good_fee", "sum"),
        adversary_cost_B=("adversary_fee", "sum"),
    ).reset_index()

    summary["malicious_jobs"] = summary["total_jobs"] - summary["benign_jobs"]
    summary["server_service_cost"] = summary["total_jobs"]
    summary["algorithm_cost_A"] = (
        summary["honest_client_fees"] + summary["server_service_cost"]
    )
    summary["B_over_A"] = (
        summary["adversary_cost_B"] / summary["algorithm_cost_A"]
    )

    return summary[
        [
            "iteration_id",
            "iteration_start",
            "iteration_last_flow",
            "total_jobs",
            "benign_jobs",
            "malicious_jobs",
            "maximum_price",
            "honest_client_fees",
            "adversary_cost_B",
            "server_service_cost",
            "algorithm_cost_A",
            "B_over_A",
        ]
    ]


# def save_weibull_fit_plots(
#     iats_seconds: np.ndarray,
#     fit: dict[str, float | int],
#     output_dir: Path,
# ) -> None:
#     shape = float(fit["shape_k"])
#     location = float(fit["location_fixed"])
#     scale = float(fit["scale_lambda_seconds"])
#
#     # Limit the visible tail so the dense part of the fit remains readable.
#     upper = float(np.quantile(iats_seconds, 0.995))
#     if upper <= 0:
#         upper = float(np.max(iats_seconds))
#     lower_positive = max(
#         float(np.min(iats_seconds)),
#         np.nextafter(0.0, 1.0),
#     )
#
#     # Linear x-axis.
#     x_linear = np.linspace(lower_positive, upper, 1000)
#     fig, ax = plt.subplots(figsize=(10, 6))
#     ax.hist(
#         iats_seconds[iats_seconds <= upper],
#         bins=100,
#         density=True,
#         alpha=0.35,
#         label="Empirical benign inter-arrival times",
#     )
#     ax.plot(
#         x_linear,
#         stats.weibull_min.pdf(
#             x_linear,
#             shape,
#             loc=location,
#             scale=scale,
#         ),
#         linewidth=2,
#         label="Fitted Weibull PDF",
#     )
#     ax.set_xlabel("Benign inter-arrival time (seconds)")
#     ax.set_ylabel("Probability density")
#     ax.set_title("Benign inter-arrival times and fitted Weibull model")
#     ax.grid(True, alpha=0.25)
#     ax.legend()
#     fig.tight_layout()
#     fig.savefig(output_dir / "01_weibull_fit_linear.png", dpi=200)
#     plt.close(fig)
#
#     # Log x-axis. Use geometric bins and strictly positive x values.
#     x_log = np.geomspace(lower_positive, upper, 1000)
#     bins_log = np.geomspace(lower_positive, upper, 100)
#     fig, ax = plt.subplots(figsize=(10, 6))
#     visible = iats_seconds[
#         (iats_seconds >= lower_positive) & (iats_seconds <= upper)
#     ]
#     ax.hist(
#         visible,
#         bins=bins_log,
#         density=True,
#         alpha=0.35,
#         label="Empirical benign inter-arrival times",
#     )
#     ax.plot(
#         x_log,
#         stats.weibull_min.pdf(
#             x_log,
#             shape,
#             loc=location,
#             scale=scale,
#         ),
#         linewidth=2,
#         label="Fitted Weibull PDF",
#     )
#     ax.set_xscale("log")
#     ax.set_yscale("log")
#     ax.set_xlabel("Benign inter-arrival time (seconds, log scale)")
#     ax.set_ylabel("Probability density (log scale)")
#     ax.set_title("Benign inter-arrival times and fitted Weibull model")
#     ax.grid(True, which="both", alpha=0.25)
#     ax.legend()
#     fig.tight_layout()
#     fig.savefig(output_dir / "02_weibull_fit_log.png", dpi=200)
#     plt.close(fig)


def save_cost_plots(
    checkpoints: pd.DataFrame,
    output_dir: Path,
) -> None:
    x = checkpoints["number_of_jobs"].to_numpy(dtype=float)
    experimental_ratio = checkpoints[
        "adversary_over_algorithm_B_over_A"
    ].to_numpy(dtype=float)
    theoretical_scaled = checkpoints[
        "theorem1_proxy_scaled"
    ].to_numpy(dtype=float)
    scale_constant = float(
        checkpoints["theorem1_scale_constant"].iloc[0]
    )

    # Create two versions of the experimental-versus-theoretical graph:
    # one with a linear y-axis and one with a logarithmic y-axis.
    for y_scale, filename in [
        (
                "linear",
                "adversary_over_algorithm_vs_jobs_linear.png",
        ),
        (
                "log",
                "adversary_over_algorithm_vs_jobs_log.png",
        ),
    ]:
        fig, ax = plt.subplots(figsize=(11, 6))

        if y_scale == "log":
            # Logarithmic axes cannot display zero or negative values.
            experimental_to_plot = np.where(
                np.isfinite(experimental_ratio)
                & (experimental_ratio > 0.0),
                experimental_ratio,
                np.nan,
            )

            theoretical_to_plot = np.where(
                np.isfinite(theoretical_scaled)
                & (theoretical_scaled > 0.0),
                theoretical_scaled,
                np.nan,
            )
        else:
            experimental_to_plot = experimental_ratio
            theoretical_to_plot = theoretical_scaled

        ax.plot(
            x,
            experimental_to_plot,
            linewidth=1.4,
            label="Empirical Curve",
        )

        ax.plot(
            x,
            theoretical_to_plot,
            linewidth=1.4,
            linestyle="--",
            label=(
                "Scaled Theoretical Curve"
                # rf"$c \frac{{B}}{{\sqrt{{B(g+1)}}+(g+1)}}$, "
                # rf"$c={scale_constant:.6g}$"
            ),
        )

        if y_scale == "log":
            ax.set_yscale("log")
            y_axis_text = "logarithmic y-axis"
        else:
            y_axis_text = "linear y-axis"

        ax.set_xlabel("Cumulative number of flows", fontsize=14, color="darkblue")
        ax.set_ylabel(
            "Adversary-to-algorithm cost ratio", fontsize=14, color="darkblue"
        )
        # ax.set_title(
        #     "LINEAR experiment versus constant-gamma "
        #     f"Theorem 1 trend ({y_axis_text})"
        # )

        ax.tick_params(labelsize=14)
        ax.grid(True, which="both", alpha=0.3)
        ax.legend(fontsize=14)

        fig.tight_layout()
        fig.savefig(
            output_dir / filename,
            dpi=300,
        )
        plt.close(fig)

    # fig, ax = plt.subplots(figsize=(11, 6))
    # ax.plot(
    #     x,
    #     checkpoints["adversary_cost_B"],
    #     label="Adversary cost B",
    # )
    # ax.plot(
    #     x,
    #     checkpoints["algorithm_cost_A"],
    #     label="Algorithm cost A",
    # )
    # ax.set_yscale("log")
    # ax.set_xlabel("Cumulative number of jobs")
    # ax.set_ylabel("Cumulative cost (log scale)")
    # ax.set_title("Cumulative adversary and algorithm costs")
    # ax.grid(True, which="both", alpha=0.3)
    # ax.legend()
    # fig.tight_layout()
    # fig.savefig(
    #     output_dir / "04_cumulative_A_and_B_vs_jobs.png",
    #     dpi=200,
    # )
    # plt.close(fig)


def build_overall_summary(
    trace: pd.DataFrame,
    validation: dict[str, int],
    iat_diagnostics: dict[str, int],
    weibull_fit: dict[str, float | int],
    csv_path: Path,
    benign_label: str,
    checkpoint_size: int,
) -> dict[str, object]:
    final = trace.iloc[-1]
    occupied_iterations = int(trace["iteration_id"].nunique())
    maximum_iteration_id = int(trace["iteration_id"].max())

    summary: dict[str, object] = {
        "input_csv": str(csv_path),
        "benign_label": benign_label,
        "checkpoint_size_jobs": int(checkpoint_size),
        # "experiment_type": "offline full-trace Weibull estimator baseline",
        "pricing_algorithm": "LINEAR",
        "pricing_rule": "PRICE = s + 1",
        "payment_assumption": (
            "Every flow pays exactly the current price and is serviced"
        ),
        "iteration_rule": "g_hat(I) = rho * length(I); reset when g_hat(I) >= 1",
        "iteration_length_seconds": float(
            weibull_fit["weibull_mean_iat_seconds"]
        ),
        "occupied_iterations": occupied_iterations,
        "maximum_iteration_id_including_empty_gaps": maximum_iteration_id,
        "maximum_price": int(trace["price"].max()),
        "final_honest_client_fees": int(final["cumulative_good_fees"]),
        "final_adversary_cost_B": int(final["cumulative_adversary_cost_B"]),
        "final_server_service_cost": int(final["cumulative_service_cost"]),
        "final_algorithm_cost_A": int(final["cumulative_algorithm_cost_A"]),
        "final_B_over_A": float(final["cumulative_B_over_A"]),
    }
    summary.update(validation)
    summary.update(iat_diagnostics)
    summary.update(weibull_fit)
    return summary


def save_outputs(
    output_dir: Path,
    trace: pd.DataFrame,
    checkpoints: pd.DataFrame,
    iteration_summary: pd.DataFrame,
    summary: dict[str, object],
    iats_seconds: np.ndarray,
    weibull_fit: dict[str, float | int],
    save_flow_trace: bool,
    # force_linear_y_axis: bool,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    pd.DataFrame([summary]).to_csv(
        output_dir / "experiment_summary.csv",
        index=False,
    )
    with (output_dir / "experiment_summary.json").open(
        "w",
        encoding="utf-8",
    ) as handle:
        json.dump(summary, handle, indent=2)

    checkpoints.to_csv(
        output_dir / "checkpoint_costs.csv",
        index=False,
    )
    iteration_summary.to_csv(
        output_dir / "iteration_summary.csv",
        index=False,
    )
    # pd.DataFrame(
    #     {"benign_interarrival_seconds": iats_seconds}
    # ).to_csv(
    #     output_dir / "benign_interarrival_times.csv",
    #     index=False,
    # )

    if save_flow_trace:
        columns = [
            "job_number",
            "original_row",
            "timestamp",
            "elapsed_seconds",
            "label",
            "is_good",
            "iteration_id",
            "position_in_iteration",
            "price",
            "good_fee",
            "adversary_fee",
            "cumulative_good_fees",
            "cumulative_adversary_cost_B",
            "cumulative_service_cost",
            "cumulative_algorithm_cost_A",
            "cumulative_B_over_A",
        ]
        trace[columns].to_csv(
            output_dir / "flow_pricing_trace.csv",
            index=False,
        )

    # save_weibull_fit_plots(iats_seconds, weibull_fit, output_dir)
    save_cost_plots(checkpoints, output_dir)


def print_summary(summary: dict[str, object], output_dir: Path) -> None:
    print("\nExperiment completed")
    print("=" * 72)
    print(f"Rows used:                 {summary['rows_used']:,}")
    print(f"Good flows:                {summary['good_flows']:,}")
    print(f"Bad flows:                 {summary['bad_flows']:,}")
    print(f"Weibull shape k:           {summary['shape_k']:.12g}")
    print(f"Weibull scale lambda (s):  {summary['scale_lambda_seconds']:.12g}")
    print(f"Weibull mean E[X] (s):     {summary['weibull_mean_iat_seconds']:.12g}")
    print(
        "Estimated good rate rho:    "
        f"{summary['rho_expected_good_flows_per_second']:.12g} flows/s"
    )
    print(f"Occupied iterations:       {summary['occupied_iterations']:,}")
    print(f"Maximum LINEAR price:      {summary['maximum_price']:,}")
    print(f"Final adversary cost B:    {summary['final_adversary_cost_B']:,}")
    print(f"Final algorithm cost A:    {summary['final_algorithm_cost_A']:,}")
    print(f"Final B/A:                 {summary['final_B_over_A']:.12g}")
    print(
        "Theorem 1 scale c:         "
        f"{summary['theorem1_scale_constant']:.12g}"
    )
    print(
        "Final scaled proxy:        "
        f"{summary['final_theorem1_proxy_scaled']:.12g}"
    )
    print(f"Outputs:                   {output_dir.resolve()}")


def main() -> None:
    args = parse_arguments()

    if args.checkpoint_size <= 0:
        raise ValueError("--checkpoint-size must be positive.")

    csv_path = Path(args.csv_file)
    if not csv_path.is_file():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    output_dir = Path(args.output_dir)

    flows, validation = load_and_sort_flows(
        csv_path=csv_path,
        timestamp_column=args.timestamp_column.strip(),
        label_column=args.label_column.strip(),
        benign_label=args.benign_label,
        read_mode=args.read_mode,
        chunk_threshold_mb=args.chunk_threshold_mb,
        chunk_size=args.chunk_size,
    )

    benign_iats, iat_diagnostics = calculate_benign_iats(flows)
    weibull_fit = fit_weibull(benign_iats)

    trace = assign_linear_prices(
        flows,
        iteration_length_seconds=float(
            weibull_fit["weibull_mean_iat_seconds"]
        ),
    )
    checkpoints = create_checkpoints(trace, args.checkpoint_size)
    checkpoints, theorem1_scale_constant = add_theorem1_comparison(
        checkpoints
    )
    iteration_summary = create_iteration_summary(trace)

    summary = build_overall_summary(
        trace=trace,
        validation=validation,
        iat_diagnostics=iat_diagnostics,
        weibull_fit=weibull_fit,
        csv_path=csv_path,
        benign_label=args.benign_label,
        checkpoint_size=args.checkpoint_size,
    )
    summary["theorem1_proxy_description"] = (
        "Constant-gamma shape: B / (sqrt(B * (g + 1)) + (g + 1))"
    )
    summary["theorem1_scaling_method"] = (
        "One constant chosen so scaled proxy equals experimental B/A "
        "at the final valid checkpoint"
    )
    summary["theorem1_scale_constant"] = theorem1_scale_constant
    summary["final_theorem1_proxy_raw"] = float(
        checkpoints["theorem1_proxy_raw"].iloc[-1]
    )
    summary["final_theorem1_proxy_scaled"] = float(
        checkpoints["theorem1_proxy_scaled"].iloc[-1]
    )

    save_outputs(
        output_dir=output_dir,
        trace=trace,
        checkpoints=checkpoints,
        iteration_summary=iteration_summary,
        summary=summary,
        iats_seconds=benign_iats,
        weibull_fit=weibull_fit,
        save_flow_trace=args.save_flow_trace,
    )
    print_summary(summary, output_dir)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
