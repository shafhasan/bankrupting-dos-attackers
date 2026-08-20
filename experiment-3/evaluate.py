from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


def numeric_column(df: pd.DataFrame, name: str, fallback: float | pd.Series = 0.0) -> pd.Series:
    if name in df.columns:
        values = pd.to_numeric(df[name], errors="coerce")
    else:
        values = pd.Series(pd.NA, index=df.index, dtype="Float64")
    if isinstance(fallback, pd.Series):
        return values.fillna(pd.to_numeric(fallback, errors="coerce").fillna(0.0))
    return values.fillna(float(fallback))


def safe_mean(series: pd.Series) -> float | None:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    return float(clean.mean()) if len(clean) else None


def safe_quantile(series: pd.Series, q: float) -> float | None:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    return float(clean.quantile(q)) if len(clean) else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Join neutral pricing logs with offline CSV ground truth")
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    run_dir = Path(args.run_dir)

    server = pd.read_csv(run_dir / "server_jobs.csv", low_memory=False)
    truth = pd.read_csv(run_dir / "evaluation_ground_truth.csv", low_memory=False)
    merged = server.merge(
        truth[["flow_uid", "Label"]],
        on="flow_uid",
        how="left",
        validate="one_to_one",
    )
    if merged["Label"].isna().any():
        missing = int(merged["Label"].isna().sum())
        raise ValueError(f"{missing} server jobs could not be joined to ground truth")

    if "algorithm" not in merged.columns:
        merged["algorithm"] = "linear"
    algorithm = str(merged["algorithm"].iloc[0]) if len(merged) else "linear"

    required_price = numeric_column(merged, "price", 0.0)
    attached_fee = numeric_column(merged, "attached_fee", required_price)
    accepted_fee = numeric_column(merged, "accepted_fee", attached_fee)
    if "submitted_fee_sum" in merged.columns:
        submitted_fee_sum = numeric_column(merged, "submitted_fee_sum", accepted_fee)
    elif "total_fee_paid" in merged.columns:
        submitted_fee_sum = numeric_column(merged, "total_fee_paid", accepted_fee)
    else:
        submitted_fee_sum = accepted_fee.copy()

    merged["price"] = required_price
    merged["attached_fee"] = attached_fee
    merged["accepted_fee"] = accepted_fee
    merged["submitted_fee_sum"] = submitted_fee_sum
    merged["attempt_count"] = numeric_column(merged, "attempt_count", 1).astype(int)
    if "rejection_count" in merged.columns:
        merged["rejection_count"] = numeric_column(merged, "rejection_count", 0).astype(int)
    else:
        merged["rejection_count"] = numeric_column(merged, "bounced_attempts", 0).astype(int)
    merged["bounced_attempts"] = merged["rejection_count"]
    merged["overpayment"] = numeric_column(merged, "overpayment", 0.0)
    merged["service_cost"] = numeric_column(merged, "service_cost", 1.0)

    merged["is_good"] = (
        merged["Label"].astype("string").str.strip().str.casefold().eq("benign")
    )

    # Requested custom charging rule:
    #   * good jobs pay only the fee on the successful attempt;
    #   * bad jobs retain cumulative submitted-fee accounting.
    merged["charged_fee"] = merged["submitted_fee_sum"]
    merged.loc[merged["is_good"], "charged_fee"] = merged.loc[merged["is_good"], "accepted_fee"]
    merged["fee_paid"] = merged["charged_fee"]
    merged["good_fee"] = merged["charged_fee"].where(merged["is_good"], 0.0)
    merged["bad_fee"] = merged["charged_fee"].where(~merged["is_good"], 0.0)
    merged["algorithm_cost_increment"] = merged["service_cost"] + merged["good_fee"]
    merged["adversary_cost_increment"] = merged["bad_fee"]
    merged["algorithm_cost"] = merged["algorithm_cost_increment"].cumsum()
    merged["adversary_cost"] = merged["adversary_cost_increment"].cumsum()
    merged["B_over_A"] = merged["adversary_cost"] / merged["algorithm_cost"].replace(0, pd.NA)
    merged.to_csv(run_dir / "priced_flows_with_ground_truth.csv", index=False)

    replay = pd.read_csv(run_dir / "controller_replay_log.csv", low_memory=False)
    good_metrics_path = run_dir / "good_flow_completion_metrics.csv"
    good_metrics = pd.read_csv(good_metrics_path, low_memory=False) if good_metrics_path.exists() else pd.DataFrame()

    good_completed = pd.Series(dtype=bool)
    good_timed_out = pd.Series(dtype=bool)
    if len(good_metrics):
        good_completed = good_metrics["status"].astype(str).eq("ok")
        good_timed_out = good_metrics["timed_out"].astype(str).str.casefold().eq("true")

    summary = {
        "algorithm": algorithm,
        "good_fee_policy": "accepted fee only; rejected good-flow fees are not charged",
        "bad_fee_policy": "all submitted bad-flow fees are charged",
        "serviced_flows": int(len(merged)),
        "good_flows_serviced": int(merged["is_good"].sum()),
        "bad_flows_serviced": int((~merged["is_good"]).sum()),
        "evaluation_flows": int(len(replay)),
        "failed_or_timed_out_flows": int((replay["status"] != "ok").sum()),
        "evaluation_unique_protocol_count": int(truth["Protocol"].nunique(dropna=False)),
        "evaluation_protocol_counts": {
            str(k): int(v) for k, v in truth["Protocol"].value_counts(dropna=False).items()
        },
        "serviced_protocol_counts": {
            str(k): int(v) for k, v in merged["protocol"].value_counts(dropna=False).items()
        },
        "pricing_scope": "global",
        "pricing_states": int(merged["logical_server_key"].nunique()) if len(merged) else 0,
        "destination_ips_observed": int(merged["destination_ip"].nunique()) if len(merged) else 0,
        "final_algorithm_cost_A": float(merged["algorithm_cost"].iloc[-1]) if len(merged) else 0.0,
        "final_adversary_cost_B": float(merged["adversary_cost"].iloc[-1]) if len(merged) else 0.0,
        "final_B_over_A": float(merged["B_over_A"].iloc[-1]) if len(merged) and pd.notna(merged["B_over_A"].iloc[-1]) else 0.0,
        "mean_good_required_price": safe_mean(merged.loc[merged["is_good"], "price"]),
        "mean_bad_required_price": safe_mean(merged.loc[~merged["is_good"], "price"]),
        "mean_good_accepted_fee": safe_mean(merged.loc[merged["is_good"], "accepted_fee"]),
        "mean_bad_submitted_fee_sum": safe_mean(merged.loc[~merged["is_good"], "submitted_fee_sum"]),
        "total_good_charged_fee": float(merged.loc[merged["is_good"], "charged_fee"].sum()),
        "total_bad_charged_fee": float(merged.loc[~merged["is_good"], "charged_fee"].sum()),
        "total_good_submitted_fee_sum": float(merged.loc[merged["is_good"], "submitted_fee_sum"].sum()),
        "total_good_rejected_fee_not_charged": float(
            (
                merged.loc[merged["is_good"], "submitted_fee_sum"]
                - merged.loc[merged["is_good"], "accepted_fee"]
            ).sum()
        ),
        "total_request_attempts_for_serviced_flows": int(merged["attempt_count"].sum()),
        "total_rejections_for_serviced_flows": int(merged["rejection_count"].sum()),
        "good_rejections_for_serviced_flows": int(merged.loc[merged["is_good"], "rejection_count"].sum()),
        "bad_rejections_for_serviced_flows": int(merged.loc[~merged["is_good"], "rejection_count"].sum()),
        "total_overpayment_on_serviced_attempts": float(merged["overpayment"].sum()),
        "good_flows_observed": int(len(good_metrics)),
        "good_flows_completed": int(good_completed.sum()) if len(good_metrics) else 0,
        "good_flows_timed_out": int(good_timed_out.sum()) if len(good_metrics) else 0,
        "good_completion_rate": float(good_completed.mean()) if len(good_metrics) else None,
        "mean_good_completion_latency_trace_seconds": safe_mean(
            good_metrics.loc[good_completed, "completion_latency_trace_seconds"]
        ) if len(good_metrics) else None,
        "p95_good_completion_latency_trace_seconds": safe_quantile(
            good_metrics.loc[good_completed, "completion_latency_trace_seconds"], 0.95
        ) if len(good_metrics) else None,
        "mean_good_completion_latency_wall_seconds": safe_mean(
            good_metrics.loc[good_completed, "completion_latency_wall_seconds"]
        ) if len(good_metrics) else None,
        "mean_good_rejections": safe_mean(good_metrics["rejection_count"]) if len(good_metrics) else None,
        "max_good_rejections": int(pd.to_numeric(good_metrics["rejection_count"], errors="coerce").max()) if len(good_metrics) else 0,
        "mean_good_final_accepted_fee": safe_mean(
            good_metrics.loc[good_completed, "final_accepted_fee"]
        ) if len(good_metrics) else None,
    }
    (run_dir / "evaluation_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # Per-client and per-destination CSV summaries are intentionally not written.
    # The paper-facing aggregate results are retained in evaluation_summary.json.
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
