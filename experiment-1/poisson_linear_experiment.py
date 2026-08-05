#!/usr/bin/env python3
"""Synthetic Poisson-arrival experiment for the LINEAR pricing algorithm.

The benign (good) flows form a Poisson process. Equivalently, their
inter-arrival times are i.i.d. exponential random variables. Malicious flows
are generated separately and then merged chronologically with the good flows.

LINEAR never sees a flow label while assigning a price. Labels are used only
after a price is assigned to calculate the experimental costs A and B.

LINEAR iterations follow a fixed mathematical time grid. If no flow arrives
for one or more iteration lengths, every intervening iteration is still
recorded, including completely empty iterations.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import textwrap
from dataclasses import asdict, dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from scipy.stats import kstest, poisson


@dataclass(frozen=True)
class ExperimentConfig:
    total_flows: int = 1_000_000
    good_fraction: float = 0.20
    good_rate: float = 100.0
    bad_pattern: str = "poisson"
    burst_size: int = 1_000
    burst_fraction: float = 0.80
    checkpoint_step: int = 100
    x_log_threshold: float = 100_000.0
    y_log_threshold: float = 100_000.0
    seed: int = 42
    output_dir: str = "poisson_linear_results"


def parse_args(
    argv: list[str] | None = None,
) -> tuple[ExperimentConfig, list[tuple[str, object]]]:
    raw_args = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        description="Generate synthetic Poisson good traffic and evaluate LINEAR."
    )
    parser.add_argument("--total-flows", type=int, default=1_000_000)
    parser.add_argument(
        "--good-fraction",
        type=float,
        default=0.20,
        help="Fraction of all flows that are good (default: 0.20).",
    )
    parser.add_argument(
        "--good-rate",
        type=float,
        default=100.0,
        help="True Poisson rate of good flows in flows/second (default: 100).",
    )
    parser.add_argument(
        "--bad-pattern",
        choices=("burst", "poisson", "constant"),
        default="poisson",
        help=(
            "Timestamp pattern used by malicious flows. The burst pattern "
            "combines attack bursts with constant-rate background bad traffic "
            "(default: poisson)."
        ),
    )
    parser.add_argument("--burst-size", type=int, default=1_000)
    parser.add_argument(
        "--burst-fraction",
        type=float,
        default=0.80,
        help=(
            "For --bad-pattern burst, fraction of bad flows assigned to bursts; "
            "the remainder are spread uniformly as background traffic "
            "(default: 0.80)."
        ),
    )
    parser.add_argument(
        "--checkpoint-step",
        type=int,
        default=100,
        help="Record costs after every this many cumulative total flows.",
    )
    parser.add_argument(
        "--x-log-threshold",
        type=float,
        default=100_000.0,
        help=(
            "Use a logarithmic x-axis when the largest plotted cumulative "
            "flow count exceeds this value (default: 100000)."
        ),
    )
    parser.add_argument(
        "--y-log-threshold",
        type=float,
        default=100.0,
        help=(
            "Use a logarithmic y-axis when the largest plotted performance "
            "value exceeds this value (default: 100)."
        ),
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--output-dir", type=str, default="poisson_linear_results"
    )
    args = parser.parse_args(raw_args)

    config = ExperimentConfig(**vars(args))
    if config.total_flows < 2:
        parser.error("--total-flows must be at least 2")
    if not 0.0 < config.good_fraction < 1.0:
        parser.error("--good-fraction must be strictly between 0 and 1")
    if config.good_rate <= 0.0:
        parser.error("--good-rate must be positive")
    if config.burst_size < 1:
        parser.error("--burst-size must be at least 1")
    if not 0.0 <= config.burst_fraction <= 1.0:
        parser.error("--burst-fraction must be between 0 and 1")
    if config.checkpoint_step < 1:
        parser.error("--checkpoint-step must be at least 1")
    if config.x_log_threshold <= 0.0:
        parser.error("--x-log-threshold must be positive")
    if config.y_log_threshold <= 0.0:
        parser.error("--y-log-threshold must be positive")

    # Retain only options explicitly written on the command line. This keeps
    # plot annotations from listing defaults the user did not specify.
    option_to_dest = {
        option: action.dest
        for action in parser._actions
        for option in action.option_strings
    }
    supplied_parameters: list[tuple[str, object]] = []
    seen_destinations: set[str] = set()
    for token in raw_args:
        option = token.split("=", maxsplit=1)[0]
        destination = option_to_dest.get(option)
        if destination is None or destination in seen_destinations:
            continue
        supplied_parameters.append((option, getattr(args, destination)))
        seen_destinations.add(destination)

    return config, supplied_parameters


def format_plot_parameters(
    supplied_parameters: list[tuple[str, object]],
    line_width: int = 105,
) -> str:
    """Format only explicitly supplied CLI options for a plot subtitle."""
    if not supplied_parameters:
        return ""

    entries = []
    for option, value in supplied_parameters:
        if isinstance(value, float):
            formatted_value = f"{value:g}"
        else:
            formatted_value = str(value)
        entries.append(f"{option}={formatted_value}")

    return textwrap.fill(
        "Run parameters: " + ", ".join(entries),
        width=line_width,
        subsequent_indent="  ",
        break_long_words=False,
        break_on_hyphens=False,
    )


def generate_good_timestamps(
    rng: np.random.Generator, number_good: int, good_rate: float
) -> np.ndarray:
    """Generate the first number_good arrivals of a Poisson process."""
    good_iats = rng.exponential(scale=1.0 / good_rate, size=number_good)
    return np.cumsum(good_iats, dtype=np.float64)


def generate_bad_timestamps(
    rng: np.random.Generator,
    number_bad: int,
    duration: float,
    pattern: str,
    burst_size: int,
    burst_fraction: float,
) -> tuple[np.ndarray, dict[str, int]]:
    """Generate malicious timestamps without affecting the good process."""
    if number_bad == 0:
        return np.empty(0, dtype=np.float64), {
            "number_burst_bad": 0,
            "number_background_bad": 0,
            "number_bursts": 0,
        }

    duration = max(duration, np.finfo(float).eps)

    if pattern == "poisson":
        # This rate makes the expected malicious trace duration equal to the
        # observed duration of the independently generated good trace.
        bad_rate = number_bad / duration
        bad_iats = rng.exponential(scale=1.0 / bad_rate, size=number_bad)
        return np.cumsum(bad_iats, dtype=np.float64), {
            "number_burst_bad": 0,
            "number_background_bad": number_bad,
            "number_bursts": 0,
        }

    if pattern == "constant":
        timestamps = np.linspace(0.0, duration, number_bad, endpoint=False)
        return timestamps, {
            "number_burst_bad": 0,
            "number_background_bad": number_bad,
            "number_bursts": 0,
        }

    # Hybrid burst attack. Keep the total malicious-flow budget fixed, assign
    # burst_fraction of it to concentrated bursts, and spread the rest across
    # the full trace as constant-rate background malicious traffic.
    number_burst_bad = int(round(number_bad * burst_fraction))
    number_burst_bad = min(max(number_burst_bad, 0), number_bad)
    number_background_bad = number_bad - number_burst_bad

    if number_background_bad > 0:
        background_spacing = duration / number_background_bad
        background_timestamps = (
            np.arange(number_background_bad, dtype=np.float64) + 0.5
        ) * background_spacing
    else:
        background_timestamps = np.empty(0, dtype=np.float64)

    if number_burst_bad > 0:
        number_bursts = math.ceil(number_burst_bad / burst_size)
        burst_spacing = duration / number_bursts
        # Center bursts between the endpoints so neither a background flow nor
        # a burst is forced to occur exactly at time zero.
        burst_centers = (
            np.arange(number_bursts, dtype=np.float64) + 0.5
        ) * burst_spacing
        burst_timestamps = np.repeat(
            burst_centers, burst_size
        )[:number_burst_bad]

        # Tiny nonnegative jitter orders flows within each burst while keeping
        # them effectively simultaneous.
        jitter_width = burst_spacing * 1e-6
        burst_timestamps += rng.uniform(
            0.0, jitter_width, size=number_burst_bad
        )
        burst_timestamps = np.minimum(
            burst_timestamps, np.nextafter(duration, 0.0)
        )
    else:
        number_bursts = 0
        burst_timestamps = np.empty(0, dtype=np.float64)

    timestamps = np.concatenate(
        (background_timestamps, burst_timestamps)
    )
    timestamps.sort(kind="stable")
    return timestamps, {
        "number_burst_bad": number_burst_bad,
        "number_background_bad": number_background_bad,
        "number_bursts": number_bursts,
    }


def merge_traffic(
    good_timestamps: np.ndarray, bad_timestamps: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Return chronological timestamps and private evaluation labels."""
    timestamps = np.concatenate((good_timestamps, bad_timestamps))
    is_good = np.concatenate(
        (
            np.ones(good_timestamps.size, dtype=bool),
            np.zeros(bad_timestamps.size, dtype=bool),
        )
    )
    order = np.argsort(timestamps, kind="stable")
    return timestamps[order], is_good[order]


def estimate_good_rate(good_timestamps: np.ndarray) -> tuple[float, np.ndarray]:
    """Batch estimate lambda from positive good-flow inter-arrival times."""
    good_iats = np.diff(good_timestamps)
    good_iats = good_iats[good_iats > 0.0]
    if good_iats.size == 0:
        raise ValueError("At least two distinct good timestamps are required")
    estimated_rate = 1.0 / float(np.mean(good_iats))
    return estimated_rate, good_iats


def run_linear(
    timestamps: np.ndarray,
    evaluation_is_good: np.ndarray,
    estimated_good_rate: float,
    checkpoint_step: int,
) -> dict[str, np.ndarray | float | int]:
    """Replay all flows under LINEAR and collect cumulative-prefix costs.

    Pricing is label-blind. Each iteration has the fixed duration
    1 / estimated_good_rate and the first timestamp anchors the mathematical
    boundary grid. Intervals are half-open: [start, end). Therefore, a flow
    exactly on a boundary belongs to the new iteration. When an arrival occurs
    after multiple boundaries, every skipped interval is recorded as an empty
    iteration. If s earlier flows were serviced in the current iteration, the
    arriving flow's price is s + 1.

    Experimental costs:
        A = fees paid by good flows + one unit of service cost per flow
        B = fees paid by malicious flows
    """
    algorithm_cost = 0
    adversary_cost = 0
    cumulative_good = 0
    if timestamps.size == 0:
        raise ValueError("At least one timestamp is required")
    if timestamps.size != evaluation_is_good.size:
        raise ValueError(
            "timestamps and evaluation_is_good must have equal size"
        )
    if estimated_good_rate <= 0.0:
        raise ValueError("estimated_good_rate must be positive")

    serviced_in_iteration = 0
    iteration_length = 1.0 / estimated_good_rate
    iteration_origin = float(timestamps[0])
    current_iteration_number = 1
    iteration_start = iteration_origin
    iteration_end = iteration_origin + iteration_length

    checkpoint_flow: list[int] = []
    checkpoint_a: list[int] = []
    checkpoint_b: list[int] = []
    checkpoint_g: list[int] = []
    checkpoint_ratio: list[float] = []
    iteration_good_counts: list[int] = []
    iteration_bad_counts: list[int] = []
    iteration_start_times: list[float] = []
    iteration_end_times: list[float] = []
    current_iteration_good = 0
    current_iteration_bad = 0

    for index, (timestamp, is_good) in enumerate(
        zip(timestamps, evaluation_is_good), start=1
    ):
        timestamp = float(timestamp)

        # Preserve the mathematical boundary grid. The loop may execute more
        # than once when a gap spans several intervals; zero counts are then
        # written for every intervening empty iteration.
        while timestamp >= iteration_end:
            iteration_good_counts.append(current_iteration_good)
            iteration_bad_counts.append(current_iteration_bad)
            iteration_start_times.append(iteration_start)
            iteration_end_times.append(iteration_end)

            serviced_in_iteration = 0
            current_iteration_good = 0
            current_iteration_bad = 0

            current_iteration_number += 1
            iteration_start = (
                iteration_origin
                + (current_iteration_number - 1) * iteration_length
            )
            iteration_end = (
                iteration_origin + current_iteration_number * iteration_length
            )

        price = serviced_in_iteration + 1

        # The server spends one normalized unit to service every flow.
        algorithm_cost += 1

        # This label check is evaluation only; it does not affect the price.
        if bool(is_good):
            algorithm_cost += price
            cumulative_good += 1
            current_iteration_good += 1
        else:
            adversary_cost += price
            current_iteration_bad += 1

        serviced_in_iteration += 1

        if index % checkpoint_step == 0 or index == timestamps.size:
            checkpoint_flow.append(index)
            checkpoint_a.append(algorithm_cost)
            checkpoint_b.append(adversary_cost)
            checkpoint_g.append(cumulative_good)
            checkpoint_ratio.append(adversary_cost / algorithm_cost)

    iteration_good_counts.append(current_iteration_good)
    iteration_bad_counts.append(current_iteration_bad)
    iteration_start_times.append(iteration_start)
    iteration_end_times.append(iteration_end)

    number_iterations = len(iteration_good_counts)

    flows = np.asarray(checkpoint_flow, dtype=np.int64)
    cost_a = np.asarray(checkpoint_a, dtype=np.float64)
    cost_b = np.asarray(checkpoint_b, dtype=np.float64)
    good = np.asarray(checkpoint_g, dtype=np.int64)
    ratio = np.asarray(checkpoint_ratio, dtype=np.float64)

    # Full Theorem 1 proxy used in the preceding experiment:
    # B / (sqrt(B(g+1)) + (g+1)).
    denominator = np.sqrt(cost_b * (good + 1.0)) + (good + 1.0)
    theorem_raw = np.divide(
        cost_b,
        denominator,
        out=np.zeros_like(cost_b),
        where=denominator > 0.0,
    )

    # Use one global multiplicative constant calculated at the final point.
    if theorem_raw[-1] > 0.0:
        theorem_scale = float(ratio[-1] / theorem_raw[-1])
    else:
        theorem_scale = 0.0
    theorem_scaled = theorem_scale * theorem_raw
    return {
        "flows": flows,
        "algorithm_cost": cost_a,
        "adversary_cost": cost_b,
        "cumulative_good": good,
        "cost_ratio": ratio,
        "theorem_raw": theorem_raw,
        "theorem_scaled": theorem_scaled,
        "theorem_scale": theorem_scale,
        "iterations": number_iterations,
        "iteration_number": np.arange(
            1, number_iterations + 1, dtype=np.int64
        ),
        "iteration_start_time": np.asarray(
            iteration_start_times, dtype=np.float64
        ),
        "iteration_end_time": np.asarray(
            iteration_end_times, dtype=np.float64
        ),
        "iteration_good": np.asarray(iteration_good_counts, dtype=np.int64),
        "iteration_bad": np.asarray(iteration_bad_counts, dtype=np.int64),
    }


def poisson_diagnostics(
    good_timestamps: np.ndarray,
    good_iats: np.ndarray,
    estimated_rate: float,
) -> dict[str, float | int | np.ndarray]:
    """Compute diagnostics for exponential gaps and Poisson window counts."""
    ks_result = kstest(good_iats, "expon", args=(0.0, 1.0 / estimated_rate))

    # About 10 expected good arrivals per window gives a useful count PMF plot.
    window_width = 10.0 / estimated_rate
    final_time = float(good_timestamps[-1])
    edges = np.arange(0.0, final_time + window_width, window_width)
    if edges.size < 2:
        edges = np.array([0.0, window_width])
    window_counts, _ = np.histogram(good_timestamps, bins=edges)
    count_mean = float(np.mean(window_counts))
    count_variance = float(np.var(window_counts, ddof=1))
    fano_factor = count_variance / count_mean if count_mean > 0.0 else math.nan

    return {
        "ks_statistic": float(ks_result.statistic),
        "ks_pvalue_descriptive": float(ks_result.pvalue),
        "count_window_seconds": window_width,
        "number_count_windows": int(window_counts.size),
        "window_count_mean": count_mean,
        "window_count_variance": count_variance,
        "window_count_fano_factor": fano_factor,
        "window_counts": window_counts,
    }


def save_checkpoint_csv(output_path: Path, results: dict[str, object]) -> None:
    columns = (
        "flows",
        "cumulative_good",
        "algorithm_cost",
        "adversary_cost",
        "cost_ratio",
        "theorem_raw",
        "theorem_scaled",
    )
    arrays = [np.asarray(results[name]) for name in columns]
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(zip(*arrays))


def save_iteration_csv(output_path: Path, results: dict[str, object]) -> None:
    """Save every mathematical LINEAR interval, including empty ones."""
    iteration_good = np.asarray(results["iteration_good"])
    iteration_bad = np.asarray(results["iteration_bad"])
    columns = (
        "iteration_number",
        "iteration_start_time",
        "iteration_end_time",
    )
    arrays = [np.asarray(results[name]) for name in columns]
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            (
                *columns,
                "good_jobs",
                "bad_jobs",
                "total_jobs",
                "contains_both",
                "has_no_good",
                "is_empty",
            )
        )
        writer.writerows(
            zip(
                *arrays,
                iteration_good,
                iteration_bad,
                iteration_good + iteration_bad,
                (iteration_good > 0) & (iteration_bad > 0),
                iteration_good == 0,
                (iteration_good + iteration_bad) == 0,
            )
        )


def plot_performance(
    output_path: Path,
    results: dict[str, object],
    supplied_parameters: list[tuple[str, object]],
    x_log_threshold: float,
    y_log_threshold: float,
) -> None:
    flows = np.asarray(results["flows"])
    ratio = np.asarray(results["cost_ratio"])
    theory = np.asarray(results["theorem_scaled"])

    use_log_x = float(np.max(flows)) > x_log_threshold
    largest_y = float(np.max(np.concatenate((ratio, theory))))
    use_log_y = largest_y > y_log_threshold

    # A true logarithmic axis cannot display zero or negative values. Preserve
    # the original result arrays and hide only those points when log-y is used.
    plotted_ratio = np.where(ratio > 0.0, ratio, np.nan) if use_log_y else ratio
    plotted_theory = (
        np.where(theory > 0.0, theory, np.nan) if use_log_y else theory
    )

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.plot(
        flows,
        plotted_ratio,
        color="tab:blue",
        linewidth=1.4,
        label="Empirical Curve",
    )
    ax.plot(
        flows,
        plotted_theory,
        color="tab:orange",
        linewidth=1.4,
        linestyle="--",
        label="Scaled Theoretical Curve",
    )
    if use_log_x:
        ax.set_xscale("log")
    # if use_log_y:
    #     ax.set_yscale("log")
    ax.set_xlabel("Cumulative number of flows", fontsize=16)
    ax.set_ylabel("Adversary-to-algorithm cost ratio (B/A)", fontsize=16)
    ax.tick_params(axis="both", labelsize=16, colors="darkblue")
    plot_title = "LINEAR on synthetic Poisson good-flow arrivals"
    parameter_text = format_plot_parameters(supplied_parameters)
    if parameter_text:
        plot_title += "\n" + parameter_text
    # ax.set_title(plot_title)
    ax.grid(alpha=0.25)
    ax.legend(fontsize=16)
    fig.tight_layout()
    fig.savefig(output_path, dpi=100)
    plt.close(fig)


# def plot_poisson_validation(
#     output_path: Path,
#     good_iats: np.ndarray,
#     estimated_rate: float,
#     diagnostics: dict[str, object],
#     rng: np.random.Generator,
#     supplied_parameters: list[tuple[str, object]],
# ) -> None:
#     # Sampling only affects plotting speed; diagnostics use every good gap.
#     sample_size = min(100_000, good_iats.size)
#     if sample_size < good_iats.size:
#         sample = rng.choice(good_iats, size=sample_size, replace=False)
#     else:
#         sample = good_iats
#
#     upper = float(np.quantile(sample, 0.995))
#     x = np.linspace(0.0, upper, 500)
#
#     fig, axes = plt.subplots(1, 2, figsize=(13, 5.5))
#     axes[0].hist(
#         sample,
#         bins=80,
#         range=(0.0, upper),
#         density=True,
#         alpha=0.65,
#         color="tab:blue",
#         label="Generated good-flow gaps",
#     )
#     axes[0].plot(
#         x,
#         estimated_rate * np.exp(-estimated_rate * x),
#         color="black",
#         linewidth=2.0,
#         label="Fitted exponential PDF",
#     )
#     axes[0].set_xlabel("Good-flow inter-arrival time (seconds)")
#     axes[0].set_ylabel("Density")
#     axes[0].set_title("Inter-arrival-time check")
#     axes[0].legend()
#     axes[0].grid(alpha=0.2)
#
#     counts = np.asarray(diagnostics["window_counts"], dtype=np.int64)
#     maximum_shown = int(np.quantile(counts, 0.999))
#     support = np.arange(0, maximum_shown + 1)
#     frequencies = np.bincount(counts, minlength=maximum_shown + 1)[: maximum_shown + 1]
#     empirical_pmf = frequencies / counts.size
#     poisson_mean = float(diagnostics["window_count_mean"])
#     axes[1].bar(
#         support,
#         empirical_pmf,
#         alpha=0.60,
#         color="tab:green",
#         label="Empirical window counts",
#     )
#     axes[1].plot(
#         support,
#         poisson.pmf(support, mu=poisson_mean),
#         color="black",
#         marker="o",
#         markersize=3,
#         linewidth=1.5,
#         label=f"Poisson PMF (mean={poisson_mean:.2f})",
#     )
#     axes[1].set_xlabel("Good flows per fixed window")
#     axes[1].set_ylabel("Probability")
#     axes[1].set_title("Poisson count check")
#     axes[1].legend()
#     axes[1].grid(alpha=0.2)
#
#     parameter_text = format_plot_parameters(supplied_parameters, line_width=130)
#     if parameter_text:
#         fig.suptitle(parameter_text, fontsize=10)
#         fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.92))
#     else:
#         fig.tight_layout()
#     fig.savefig(output_path, dpi=220)
#     plt.close(fig)


def main() -> None:
    config, supplied_parameters = parse_args()
    output_dir = Path(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(config.seed)
    number_good = int(round(config.total_flows * config.good_fraction))
    number_good = min(max(number_good, 2), config.total_flows - 1)
    number_bad = config.total_flows - number_good

    good_timestamps = generate_good_timestamps(rng, number_good, config.good_rate)
    bad_timestamps, bad_generation = generate_bad_timestamps(
        rng=rng,
        number_bad=number_bad,
        duration=float(good_timestamps[-1]),
        pattern=config.bad_pattern,
        burst_size=config.burst_size,
        burst_fraction=config.burst_fraction,
    )
    timestamps, evaluation_is_good = merge_traffic(
        good_timestamps, bad_timestamps
    )

    # This is an offline synthetic experiment: use the known-good timestamps to
    # calibrate lambda, freeze that estimate, and then replay all opaque flows.
    estimated_rate, good_iats = estimate_good_rate(good_timestamps)
    results = run_linear(
        timestamps=timestamps,
        evaluation_is_good=evaluation_is_good,
        estimated_good_rate=estimated_rate,
        checkpoint_step=config.checkpoint_step,
    )
    diagnostics = poisson_diagnostics(
        good_timestamps=good_timestamps,
        good_iats=good_iats,
        estimated_rate=estimated_rate,
    )

    save_checkpoint_csv(output_dir / "linear_checkpoints.csv", results)
    save_iteration_csv(output_dir / "iteration_composition.csv", results)
    plot_performance(
        output_dir / "linear_performance.png",
        results,
        supplied_parameters,
        x_log_threshold=config.x_log_threshold,
        y_log_threshold=config.y_log_threshold,
    )
    # plot_poisson_validation(
    #     output_dir / "poisson_validation.png",
    #     good_iats=good_iats,
    #     estimated_rate=estimated_rate,
    #     diagnostics=diagnostics,
    #     rng=rng,
    #     supplied_parameters=supplied_parameters,
    # )

    iteration_good = np.asarray(results["iteration_good"])
    iteration_bad = np.asarray(results["iteration_bad"])
    summary = {
        "configuration": asdict(config),
        "generated": {
            "number_good": number_good,
            "number_bad": number_bad,
            "trace_duration_seconds": float(timestamps[-1] - timestamps[0]),
            "true_good_rate": config.good_rate,
            "estimated_good_rate": estimated_rate,
            "good_rate_relative_error": (
                estimated_rate - config.good_rate
            )
            / config.good_rate,
            **bad_generation,
        },
        "poisson_diagnostics": {
            key: value
            for key, value in diagnostics.items()
            if key != "window_counts"
        },
        "linear": {
            "iterations": int(results["iterations"]),
            "iteration_length_seconds": 1.0 / estimated_rate,
            "iteration_grid_origin_seconds": float(timestamps[0]),
            "final_algorithm_cost_A": float(
                np.asarray(results["algorithm_cost"])[-1]
            ),
            "final_adversary_cost_B": float(
                np.asarray(results["adversary_cost"])[-1]
            ),
            "final_B_over_A": float(np.asarray(results["cost_ratio"])[-1]),
            "theorem_final_point_scale": float(results["theorem_scale"]),
            "iterations_with_both_good_and_bad": int(
                np.count_nonzero((iteration_good > 0) & (iteration_bad > 0))
            ),
            "iterations_with_no_bad": int(
                np.count_nonzero(iteration_bad == 0)
            ),
            "iterations_with_no_good": int(
                np.count_nonzero(iteration_good == 0)
            ),
            "bad_only_iterations": int(
                np.count_nonzero((iteration_good == 0) & (iteration_bad > 0))
            ),
            "completely_empty_iterations": int(
                np.count_nonzero((iteration_good + iteration_bad) == 0)
            ),
        },
        # "notes": {
        #     "pricing_is_label_blind": True,
        #     "iteration_boundaries": (
        #         "Fixed mathematical grid anchored at the first flow timestamp; "
        #         "intervals are [start, end), and skipped intervals are recorded."
        #     ),
        #     "ks_pvalue_caution": (
        #         "Descriptive only because lambda was estimated from the same gaps."
        #     ),
        # },
    }
    with (output_dir / "summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)

    print(json.dumps(summary, indent=2))
    print(f"\nResults written to: {output_dir.resolve()}")


if __name__ == "__main__":
    main()
