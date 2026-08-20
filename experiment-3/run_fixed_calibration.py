from __future__ import annotations

import runpy
import sys

if __name__ == "__main__":
    sys.argv.insert(1, "--mode")
    sys.argv.insert(2, "fixed")
    runpy.run_module("run_experiment", run_name="__main__")
