from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from common import request_json, wait_for_port


def run_one_algorithm(
    *,
    base: Path,
    args: argparse.Namespace,
    algorithm: str,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    run_succeeded = False

    server_log = (output_dir / "server_stdout.log").open("w", encoding="utf-8")
    estimator_log = (output_dir / "estimator_stdout.log").open("w", encoding="utf-8")
    server = subprocess.Popen(
        [
            sys.executable,
            str(base / "server.py"),
            "--host",
            args.host,
            "--udp-port",
            str(args.udp_port),
            "--tcp-port",
            str(args.tcp_port),
            "--control-port",
            str(args.control_port),
            "--algorithm",
            algorithm,
            "--output",
            str(output_dir / "server_jobs.csv"),
        ],
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )
    estimator = subprocess.Popen(
        [
            sys.executable,
            str(base / "estimator_service.py"),
            "--host",
            args.host,
            "--port",
            str(args.estimator_port),
        ],
        stdout=estimator_log,
        stderr=subprocess.STDOUT,
    )

    try:
        wait_for_port(args.host, args.control_port)
        wait_for_port(args.host, args.tcp_port)
        wait_for_port(args.host, args.estimator_port)
        command = [
            sys.executable,
            str(base / "trace_controller.py"),
            "--csv",
            str(Path(args.csv).resolve()),
            "--mode",
            args.mode,
            "--algorithm",
            algorithm,
            "--output-dir",
            str(output_dir),
            "--server-host",
            args.host,
            "--udp-port",
            str(args.udp_port),
            "--tcp-port",
            str(args.tcp_port),
            "--control-port",
            str(args.control_port),
            "--estimator-host",
            args.host,
            "--estimator-port",
            str(args.estimator_port),
            "--calibration-good-flows",
            str(args.calibration_good_flows),
            "--window-size",
            str(args.window_size),
            "--min-estimator-samples",
            str(args.min_estimator_samples),
            "--refit-every",
            str(args.refit_every),
            "--speedup",
            str(args.speedup),
            "--max-evaluation-flows",
            str(args.max_evaluation_flows),
            "--max-attempts",
            str(args.max_attempts),
            "--retry-delay",
            str(args.retry_delay),
            "--good-flow-timeout",
            str(args.good_flow_timeout),
            "--socket-timeout",
            str(args.socket_timeout),
        ]
        subprocess.run(command, check=True)
        flushed = request_json(args.host, args.control_port, {"command": "flush"}, timeout=10)
        if not flushed.get("ok"):
            raise RuntimeError(flushed)
        subprocess.run(
            [sys.executable, str(base / "evaluate.py"), "--run-dir", str(output_dir)],
            check=True,
        )
        plot_command = [
            sys.executable,
            str(base / "plot_b_over_a.py"),
            "--run-dir",
            str(output_dir),
            "--plot-every",
            str(args.plot_every),
        ]
        if args.theorem2_M is not None:
            plot_command.extend(["--theorem2-M", str(args.theorem2_M)])
        subprocess.run(plot_command, check=True)
        summary = json.loads((output_dir / "evaluation_summary.json").read_text(encoding="utf-8"))
        run_succeeded = True
        return summary
    finally:
        try:
            request_json(args.host, args.control_port, {"command": "shutdown"}, timeout=2)
        except Exception:
            server.terminate()
        try:
            request_json(args.host, args.estimator_port, {"command": "shutdown"}, timeout=2)
        except Exception:
            estimator.terminate()
        for process in [server, estimator]:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        server_log.close()
        estimator_log.close()

        # Keep only the paper-facing outputs after a successful run.  These
        # intermediate files are required while replay/evaluation/plotting are
        # in progress, so they are removed only after all stages succeed.
        if run_succeeded:
            intermediate_files = [
                "selection_summary.json",
                "evaluation_ground_truth.csv",
                "server_jobs.csv",
                "controller_replay_log.csv",
                "server_stdout.log",
                "estimator_stdout.log",
            ]
            for filename in intermediate_files:
                path = output_dir / filename
                if path.exists():
                    path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the same trace through LINEAR and LINEAR-POWER"
    )
    parser.add_argument("--csv", required=True)
    parser.add_argument("--mode", choices=["fixed", "sliding"], required=True)
    parser.add_argument("--output-dir", required=True)
    # Kept only so older command lines still run. These options are ignored:
    # Experiment 3 now includes every source IP in the evaluation trace.
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
    parser.add_argument("--speedup", type=float, default=0.0)
    parser.add_argument("--max-evaluation-flows", type=int, default=0)
    parser.add_argument("--plot-every", type=int, default=1)
    parser.add_argument(
        "--max-attempts",
        "--max-linear-power-attempts",
        dest="max_attempts",
        type=int,
        default=64,
        help=(
            "Maximum number of attempts allowed for a good LINEAR-POWER flow. "
            "LINEAR uses the zero-latency one-attempt baseline. "
            "--max-linear-power-attempts remains an alias."
        ),
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=0.01,
        help="Logical trace seconds between a rejected LINEAR-POWER good-flow attempt and its retry",
    )
    parser.add_argument(
        "--good-flow-timeout",
        type=float,
        default=60.0,
        help="Logical trace seconds allowed for a good flow; 0 disables the timeout",
    )
    parser.add_argument(
        "--socket-timeout",
        type=float,
        default=10.0,
        help="Real wall-clock timeout for one local socket request",
    )
    parser.add_argument(
        "--theorem2-M",
        type=float,
        default=None,
        help=(
            "Optional manual Theorem 2 M override. By default, LINEAR-POWER "
            "computes M from evaluation good-flow arrivals using --retry-delay "
            "as the experimental Delta proxy."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--udp-port", type=int, default=19017)
    parser.add_argument("--tcp-port", type=int, default=19006)
    parser.add_argument("--control-port", type=int, default=19005)
    parser.add_argument("--estimator-port", type=int, default=19100)
    args = parser.parse_args()

    base = Path(__file__).resolve().parent
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    linear_dir = output_dir / "linear"
    print("\n=== Running LINEAR ===", flush=True)
    linear_summary = run_one_algorithm(
        base=base,
        args=args,
        algorithm="linear",
        output_dir=linear_dir,
    )

    linear_power_dir = output_dir / "linear_power"
    print("\n=== Running LINEAR-POWER ===", flush=True)
    linear_power_summary = run_one_algorithm(
        base=base,
        args=args,
        algorithm="linear-power",
        output_dir=linear_power_dir,
    )

    comparison = {
        "csv": str(Path(args.csv).resolve()),
        "mode": args.mode,
        "linear_run_dir": str(linear_dir),
        "linear_power_run_dir": str(linear_power_dir),
        "LINEAR": linear_summary,
        "LINEAR_POWER": linear_power_summary,
    }
    print("\n=== Combined summary ===")
    print(json.dumps(comparison, indent=2))


if __name__ == "__main__":
    main()
