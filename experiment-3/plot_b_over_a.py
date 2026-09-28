from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


PLOT_EVERY = 500


def save_plot(
    data: pd.DataFrame,
    theoretical_label: str,
    pearson: float | None,
    output_path: Path,
    *,
    log_scale: bool,
) -> None:
    x = pd.to_numeric(data["cumulative_jobs"], errors="coerce").to_numpy(dtype=float)
    empirical = pd.to_numeric(data["B_over_A"], errors="coerce").to_numpy(dtype=float)
    theoretical = pd.to_numeric(data["theorem_proxy_raw"], errors="coerce").to_numpy(dtype=float)

    finite_x = np.isfinite(x)
    if log_scale:
        empirical_to_plot = np.where(
            finite_x & np.isfinite(empirical) & (empirical > 0.0),
            empirical,
            np.nan,
        )
        theoretical_to_plot = np.where(
            finite_x & np.isfinite(theoretical) & (theoretical > 0.0),
            theoretical,
            np.nan,
        )
    else:
        empirical_to_plot = np.where(
            finite_x & np.isfinite(empirical),
            empirical,
            np.nan,
        )
        theoretical_to_plot = np.where(
            finite_x & np.isfinite(theoretical),
            theoretical,
            np.nan,
        )

    pearson_text = "undefined" if pearson is None else f"{pearson:.4f}"

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.plot(
        x,
        empirical_to_plot,
        linewidth=1.4,
        label="Empirical Curve",
    )
    ax.plot(
        x,
        theoretical_to_plot,
        linewidth=1.4,
        linestyle="--",
        label=f"Theoretical Curve",
    )
    if log_scale:
        ax.set_yscale("log")
    ax.set_xlabel("Cumulative number of flows", fontsize=18, color="darkblue")
    ax.set_ylabel("Adversary-to-algorithm cost ratio", fontsize=18, color="darkblue")
    ax.tick_params(labelsize=18)
    ax.grid(True, which="both", alpha=0.3)
    ax.legend(fontsize=18,
              loc="best",
              title=f"Pearson r = {pearson:.4f}",
              title_fontsize=18,
    )
    fig.tight_layout()
    fig.savefig(output_path, dpi=300)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Generate raw B/A plots from the compact 500-flow checkpoint file. "
            "Pearson correlation is precomputed online from every serviced flow."
        )
    )
    parser.add_argument("--run-dir", required=True)
    parser.add_argument(
        "--plot-every",
        type=int,
        default=PLOT_EVERY,
        help="Deprecated and ignored; the controller stores one plotting checkpoint every 500 serviced flows",
    )
    parser.add_argument(
        "--theorem2-M",
        type=float,
        default=None,
        help="Deprecated here; Theorem 2 M is resolved before replay by trace_controller.py",
    )
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    points_path = run_dir / "B_over_A_plot_points.csv"
    metadata_path = run_dir / "theorem_proxy_metadata.json"

    if not points_path.exists():
        raise FileNotFoundError(f"Missing compact plotting file: {points_path}")
    if not metadata_path.exists():
        raise FileNotFoundError(f"Missing theorem metadata file: {metadata_path}")

    data = pd.read_csv(points_path, low_memory=False)
    required = {
        "cumulative_jobs",
        "B_over_A",
        "theorem_proxy_raw",
    }
    missing = required.difference(data.columns)
    if missing:
        raise ValueError(f"{points_path} is missing columns: {sorted(missing)}")

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    algorithm = str(metadata.get("algorithm", "linear")).lower()
    pearson = metadata.get("pearson_correlation")
    pearson = float(pearson) if pearson is not None else None

    if algorithm == "linear-power":
        m = metadata.get("theorem2_M")
        m_text = f"{m:g}" if m is not None else "?"
        theoretical_label = (
            r"Theorem 2 proxy: "
            r"$\frac{B}{\sqrt{B+1}\min(g+1,M\sqrt{g+1},M\sqrt{B+1})+(g+1)}$, "
            rf"$M={m_text}$"
        )
    else:
        theoretical_label = r"Theorem 1 proxy: $\frac{B}{\sqrt{B(g+1)}+(g+1)}$"

    linear_path = run_dir / "B_over_A_linear_scale.png"
    log_path = run_dir / "B_over_A_log_scale.png"

    # Remove obsolete output from older min-max/RMSE versions if the directory is reused.
    obsolete_normalized = run_dir / "B_over_A_individual_minmax.png"
    if obsolete_normalized.exists():
        obsolete_normalized.unlink()

    save_plot(
        data,
        theoretical_label,
        pearson,
        linear_path,
        log_scale=False,
    )
    save_plot(
        data,
        theoretical_label,
        pearson,
        log_path,
        log_scale=True,
    )

    print(json.dumps(metadata, indent=2))
    print(linear_path)
    print(log_path)


if __name__ == "__main__":
    main()
