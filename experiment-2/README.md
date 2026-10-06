# Trace-Driven LINEAR / Weibull Experiment — Docker

This container runs the trace-driven CIC-DDoS2019 experiment in `trace_weibull_experiment.py`.

The current code uses the first 50 chronological benign flows by default to fit the Weibull estimator, excludes the entire chronological calibration prefix through the 50th benign flow from LINEAR replay, computes Pearson correlation from every replay flow using the raw empirical and raw unscaled theoretical ratios, and saves plot/checkpoint points every 500 replay flows by default.

## Files

```text
trace-driven-docker/
├── Dockerfile
├── .dockerignore
├── docker-compose.yml
├── requirements.txt
├── run_trace.ps1
├── trace_weibull_experiment.py
├── data/       # put input CSV files here; not copied into image
└── results/    # generated outputs appear here
```

## Requirements on Windows

Install Docker Desktop and make sure Linux containers are enabled. No local Python installation is required.

## Build

Open PowerShell in this directory and run:

```powershell
docker build -t ddos-trace-experiment:latest .
```

## Run directly with Docker

Place a dataset such as `DrDoS_MSSQL.csv` in `data/`, then run:

```powershell
docker run --rm --init `
  --mount "type=bind,source=$((Resolve-Path .\data).Path),target=/data,readonly" `
  --mount "type=bind,source=$((Resolve-Path .\results).Path),target=/results" `
  ddos-trace-experiment:latest `
  /data/DrDoS_MSSQL.csv `
  --output-dir /results/MSSQL
```

The dataset is mounted read-only at `/data`. Results are written to the Windows `results/` directory.

## Run with the PowerShell helper

```powershell
.\run_trace.ps1 -Dataset "DrDoS_MSSQL.csv" -RunName "MSSQL"
```

The helper preserves the current defaults:

```text
checkpoint size       = 500 replay flows
estimator good flows  = 50 benign flows
read mode             = auto
chunk threshold       = 3078 MiB
chunk size            = 250000 rows
```

Example with explicit options:

```powershell
.\run_trace.ps1 `
  -Dataset "DrDoS_MSSQL.csv" `
  -RunName "MSSQL" `
  -CheckpointSize 500 `
  -EstimatorGoodFlows 50 `
  -ReadMode auto `
  -ChunkThresholdMB 3078 `
  -ChunkSize 250000
```

To save the full per-flow pricing trace as well:

```powershell
.\run_trace.ps1 `
  -Dataset "DrDoS_MSSQL.csv" `
  -RunName "MSSQL" `
  -SaveFlowTrace
```

## Run with Docker Compose

PowerShell:

```powershell
$env:DATASET="DrDoS_MSSQL.csv"
$env:RUN_NAME="MSSQL"
docker compose run --rm trace-experiment
```

Optional overrides:

```powershell
$env:CHECKPOINT_SIZE="500"
$env:ESTIMATOR_GOOD_FLOWS="50"
$env:READ_MODE="auto"
docker compose run --rm trace-experiment
```

## Outputs

For a run named `MSSQL`, outputs are written under `results/MSSQL/`, including:

```text
experiment_summary.csv
experiment_summary.json
checkpoint_costs.csv
iteration_summary.csv
adversary_over_algorithm_vs_jobs_linear.png
adversary_over_algorithm_vs_jobs_log.png
```

If `--save-flow-trace` is enabled, the run also writes `flow_pricing_trace.csv`.

## Notes on large CSV files

The script already supports `auto`, `full`, and `chunked` CSV loading. In `auto` mode, files at or above the configured threshold use pandas chunked parsing. This controls CSV parsing behavior; the experiment still constructs the replay trace in memory later, so Docker must have enough RAM available for large datasets.

The container does not change experiment timestamps, Weibull fitting, LINEAR iteration boundaries, pricing, costs, Pearson calculation, or checkpoint selection. It only packages the Python runtime and dependencies.
