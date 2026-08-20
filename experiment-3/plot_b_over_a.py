from __future__ import annotations

import argparse
import json
from pathlib import Path
from tkinter import font

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def load_priced_flows(run_dir: Path) -> pd.DataFrame:
    priced_path = run_dir / "priced_flows_with_ground_truth.csv"
    if priced_path.exists():
        data = pd.read_csv(priced_path, low_memory=False)
        required = {"algorithm_cost", "adversary_cost", "is_good"}
        if required.issubset(data.columns):
            return data

    server_path = run_dir / "server_jobs.csv"
    truth_path = run_dir / "evaluation_ground_truth.csv"
    if not server_path.exists():
        raise FileNotFoundError(f"Missing {server_path}")
    if not truth_path.exists():
        raise FileNotFoundError(f"Missing {truth_path}")

    server = pd.read_csv(server_path, low_memory=False)
    truth = pd.read_csv(truth_path, low_memory=False)
    data = server.merge(
        truth[["flow_uid", "Label"]],
        on="flow_uid",
        how="left",
        validate="one_to_one",
    )
    if data["Label"].isna().any():
        missing = int(data["Label"].isna().sum())
        raise ValueError(f"{missing} server jobs could not be joined to ground truth")

    data["is_good"] = (
        data["Label"].astype("string").str.strip().str.casefold().eq("benign")
    )
    required = pd.to_numeric(data["price"], errors="coerce").fillna(0.0)
    accepted = pd.to_numeric(
        data["accepted_fee"] if "accepted_fee" in data.columns else required,
        errors="coerce",
    ).fillna(required)
    if "submitted_fee_sum" in data.columns:
        submitted = pd.to_numeric(data["submitted_fee_sum"], errors="coerce").fillna(accepted)
    elif "total_fee_paid" in data.columns:
        submitted = pd.to_numeric(data["total_fee_paid"], errors="coerce").fillna(accepted)
    else:
        submitted = accepted
    fee = submitted.copy()
    fee.loc[data["is_good"]] = accepted.loc[data["is_good"]]
    data["good_fee"] = fee.where(data["is_good"], 0.0)
    data["bad_fee"] = fee.where(~data["is_good"], 0.0)
    data["algorithm_cost"] = (data["service_cost"] + data["good_fee"]).cumsum()
    data["adversary_cost"] = data["bad_fee"].cumsum()
    return data


def load_plot_metadata(run_dir: Path) -> tuple[str, str, str, float | None]:
    summary_path = run_dir / "selection_summary.json"
    dataset_name = "Unknown dataset"
    mode = "unknown"
    algorithm = "linear"
    retry_delay = None
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        csv_path = summary.get("csv")
        if csv_path:
            dataset_name = Path(csv_path).name
        mode = str(summary.get("mode", mode)).lower()
        algorithm = str(summary.get("algorithm", algorithm)).lower()
        retry_delay_value = summary.get("retry_delay_trace_seconds")
        if retry_delay_value is not None:
            retry_delay = float(retry_delay_value)
    return dataset_name, mode, algorithm, retry_delay


def compute_theorem2_m(run_dir: Path, delta: float) -> int:
    """Compute the empirical Theorem 2 M over the evaluation workload.

    M is the maximum number of *generated good jobs* whose original trace
    timestamps fall in any interval of logical length ``delta``.  Retries are
    intentionally excluded because they are retransmissions of existing jobs,
    not newly generated good jobs.

    In this experiment, ``delta`` is the configured LINEAR-POWER retry delay
    and is therefore an experimental proxy for the theorem's communication
    delay bound Delta; it is not a direct measurement of network latency.
    """
    if delta <= 0:
        raise ValueError("delta must be greater than zero to compute Theorem 2 M")

    truth_path = run_dir / "evaluation_ground_truth.csv"
    if not truth_path.exists():
        raise FileNotFoundError(f"Missing {truth_path}")

    truth = pd.read_csv(truth_path, low_memory=False)
    required = {"Timestamp", "Label"}
    missing = required.difference(truth.columns)
    if missing:
        raise ValueError(
            f"{truth_path} is missing columns required to compute Theorem 2 M: "
            f"{sorted(missing)}"
        )

    good_mask = (
        truth["Label"]
        .astype("string")
        .str.replace("\ufeff", "", regex=False)
        .str.strip()
        .str.casefold()
        .eq("benign")
    )
    good = truth.loc[good_mask, "Timestamp"].astype("string").str.strip()
    if good.empty:
        return 1

    try:
        timestamps = pd.to_datetime(good, errors="coerce", format="mixed")
    except (TypeError, ValueError):
        timestamps = pd.to_datetime(good, errors="coerce")
    timestamps = timestamps.dropna().sort_values()
    if timestamps.empty:
        return 1

    # Integer nanoseconds avoid floating-point precision loss for absolute
    # epoch timestamps.  The two-pointer window counts both endpoints when
    # their separation is exactly delta, i.e., times[right] - times[left] <= delta.
    times_ns = timestamps.astype("int64").to_numpy(dtype=np.int64)
    delta_ns = max(1, int(round(delta * 1_000_000_000.0)))

    left = 0
    max_good = 0
    for right in range(len(times_ns)):
        while times_ns[right] - times_ns[left] > delta_ns:
            left += 1
        max_good = max(max_good, right - left + 1)

    return max(1, int(max_good))


def select_plot_checkpoints(data: pd.DataFrame, plot_every: int) -> pd.DataFrame:
    if plot_every <= 0:
        raise ValueError("plot_every must be greater than zero")
    if plot_every == 1 or data.empty:
        return data.copy()
    selected = data.loc[data["cumulative_jobs"].mod(plot_every).eq(0)].copy()
    final_job = int(data["cumulative_jobs"].iloc[-1])
    if selected.empty or int(selected["cumulative_jobs"].iloc[-1]) != final_job:
        selected = pd.concat([selected, data.iloc[[-1]]], ignore_index=True)
    return selected


def build_curves(
    data: pd.DataFrame,
    algorithm: str,
    theorem2_m: float,
) -> tuple[pd.DataFrame, float, str, str]:
    data = data.copy().reset_index(drop=True)
    data["cumulative_jobs"] = np.arange(1, len(data) + 1, dtype=np.int64)
    data["cumulative_good_jobs"] = data["is_good"].astype(int).cumsum()

    b = data["adversary_cost"].to_numpy(dtype=float)
    a = data["algorithm_cost"].to_numpy(dtype=float)
    g_plus_one = data["cumulative_good_jobs"].to_numpy(dtype=float) + 1.0

    empirical = np.divide(
        b,
        a,
        out=np.full_like(b, np.nan, dtype=float),
        where=a > 0,
    )

    if algorithm == "linear-power":
        if theorem2_m <= 0:
            raise ValueError("theorem2_m must be positive")
        min_term = np.minimum.reduce(
            [
                g_plus_one,
                theorem2_m * np.sqrt(g_plus_one),
                theorem2_m * np.sqrt(b + 1.0),
            ]
        )
        theorem_cost_proxy = np.sqrt(b + 1.0) * min_term + g_plus_one
        theorem_raw = np.divide(
            b,
            theorem_cost_proxy,
            out=np.zeros_like(b, dtype=float),
            where=theorem_cost_proxy > 0,
        )
        theorem_column = "theorem_2_proxy_scaled"
        theorem_label = (
            r"Scaled Theorem 2 proxy: "
            r"$c\frac{B}{\sqrt{B+1}\min(g+1,M\sqrt{g+1},M\sqrt{B+1})+(g+1)}$, "
            rf"$M={theorem2_m:g}$"
        )
    else:
        denominator = np.sqrt(b * g_plus_one) + g_plus_one
        theorem_raw = np.divide(
            b,
            denominator,
            out=np.zeros_like(b, dtype=float),
            where=denominator > 0,
        )
        theorem_column = "theorem_1_proxy_scaled"
        theorem_label = (
            r"Scaled Theorem 1 proxy: "
            r"$c\frac{B}{\sqrt{B(g+1)}+(g+1)}$"
        )

    valid = np.isfinite(empirical) & np.isfinite(theorem_raw) & (theorem_raw > 0)
    if valid.any():
        last_valid = np.flatnonzero(valid)[-1]
        scale_constant = float(empirical[last_valid] / theorem_raw[last_valid])
    else:
        scale_constant = 1.0

    data["B_over_A"] = empirical
    data[theorem_column] = scale_constant * theorem_raw
    return data, scale_constant, theorem_column, theorem_label


def save_plot(
    data: pd.DataFrame,
    scale_constant: float,
    theorem_column: str,
    theorem_label: str,
    output_path: Path,
    plot_title: str,
    *,
    log_scale: bool,
) -> None:
    x = data["cumulative_jobs"].to_numpy(dtype=float)
    empirical = data["B_over_A"].to_numpy(dtype=float)
    theoretical = data[theorem_column].to_numpy(dtype=float)

    valid_empirical = np.isfinite(empirical)
    valid_theoretical = np.isfinite(theoretical)
    if log_scale:
        valid_empirical &= empirical > 0
        valid_theoretical &= theoretical > 0

    fig, ax = plt.subplots(figsize=(11, 6))
    ax.plot(x[valid_empirical], empirical[valid_empirical], linewidth=1.4, label="Empirical Curve")
    ax.plot(
        x[valid_theoretical],
        theoretical[valid_theoretical],
        linewidth=1.4,
        linestyle="--",
        label="Scaled Theoretical Curve")

    if log_scale:
        ax.set_yscale("log")

    # ax.set_title(plot_title)
    ax.set_xlabel("Cumulative number of flows", fontsize=14, color="darkblue")
    ax.set_ylabel("Adversary-to-algorithm cost ratio", fontsize=14, color="darkblue")
    ax.tick_params(labelsize=14)
    ax.grid(True, which="both", alpha=0.3)
    ax.legend(fontsize=14)
    fig.tight_layout()
    fig.savefig(output_path, dpi=300)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate B/A plots from an existing run")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument(
        "--plot-every",
        type=int,
        default=1,
        help="Plot one checkpoint every N cumulative flows; calculations still use every flow",
    )
    parser.add_argument(
        "--theorem2-M",
        type=float,
        default=None,
        help=(
            "Optional manual override for Theorem 2 M. If omitted for "
            "LINEAR-POWER, M is computed automatically from original good-flow "
            "arrivals using the configured retry delay as the Delta proxy."
        ),
    )
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    dataset_name, mode, algorithm, retry_delay = load_plot_metadata(run_dir)
    mode_label = "Fixed iteration length" if mode == "fixed" else "Sliding-window iteration length"
    algorithm_label = "LINEAR-POWER" if algorithm == "linear-power" else "LINEAR"
    theorem_label_short = "Theorem 2" if algorithm == "linear-power" else "Theorem 1"
    plot_title = (
        f"{dataset_name} — {algorithm_label}, {mode_label}\n"
        f"Empirical B/A versus scaled {theorem_label_short} trend"
    )

    if algorithm == "linear-power":
        if args.theorem2_M is not None:
            if args.theorem2_M <= 0:
                raise ValueError("--theorem2-M must be positive")
            theorem2_m = float(args.theorem2_M)
            theorem2_m_source = "manual override"
            theorem2_delta_proxy = retry_delay
        else:
            if retry_delay is None or retry_delay <= 0:
                raise ValueError(
                    "Cannot automatically compute Theorem 2 M because "
                    "retry_delay_trace_seconds is missing or non-positive. "
                    "Use a positive --retry-delay or supply --theorem2-M manually."
                )
            theorem2_delta_proxy = float(retry_delay)
            theorem2_m = float(compute_theorem2_m(run_dir, theorem2_delta_proxy))
            theorem2_m_source = "computed from evaluation good-flow arrivals"

        print(
            "Theorem 2 proxy parameters: "
            f"Delta proxy={theorem2_delta_proxy:g} trace seconds, "
            f"M={theorem2_m:g} ({theorem2_m_source})"
        )
    else:
        theorem2_m = 1.0  # unused by the LINEAR/Theorem 1 branch
        theorem2_m_source = "not used by LINEAR"
        theorem2_delta_proxy = None

    data = load_priced_flows(run_dir)
    curves, scale_constant, theorem_column, theorem_label = build_curves(
        data,
        algorithm=algorithm,
        theorem2_m=theorem2_m,
    )
    plot_curves = select_plot_checkpoints(curves, args.plot_every)

    linear_path = run_dir / "B_over_A_linear_scale.png"
    log_path = run_dir / "B_over_A_log_scale.png"

    columns = [
        "cumulative_jobs",
        "cumulative_good_jobs",
        "algorithm_cost",
        "adversary_cost",
        "B_over_A",
        theorem_column,
    ]
    proxy_metadata = {
        "algorithm": algorithm,
        "theorem": "Theorem 2" if algorithm == "linear-power" else "Theorem 1",
        "scale_constant": scale_constant,
        "theorem2_M": theorem2_m if algorithm == "linear-power" else None,
        "theorem2_M_source": theorem2_m_source if algorithm == "linear-power" else None,
        "theorem2_delta_proxy_trace_seconds": (
            theorem2_delta_proxy if algorithm == "linear-power" else None
        ),
        "theorem2_delta_proxy_definition": (
            "configured retry delay; experimental proxy for theoretical Delta"
            if algorithm == "linear-power"
            else None
        ),
    }
    (run_dir / "theorem_proxy_metadata.json").write_text(
        json.dumps(proxy_metadata, indent=2),
        encoding="utf-8",
    )

    save_plot(
        plot_curves,
        scale_constant,
        theorem_column,
        theorem_label,
        linear_path,
        plot_title,
        log_scale=False,
    )
    save_plot(
        plot_curves,
        scale_constant,
        theorem_column,
        theorem_label,
        log_path,
        plot_title,
        log_scale=True,
    )

    print(linear_path)
    print(log_path)


if __name__ == "__main__":
    main()
