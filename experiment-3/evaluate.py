from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Compatibility helper for the low-I/O experiment. Aggregate evaluation "
            "is now performed online by trace_controller.py."
        )
    )
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    summary_path = run_dir / "evaluation_summary.json"
    if not summary_path.exists():
        raise FileNotFoundError(
            f"Missing {summary_path}. Run trace_controller.py/run_experiment.py first."
        )

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
