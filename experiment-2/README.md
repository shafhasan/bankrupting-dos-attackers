# Trace-Driven CIC-DDoS2019 Experiment for LINEAR

This repository contains the Dockerized trace-driven experiment used to evaluate the **LINEAR** pricing algorithm on CIC-DDoS2019 flow traces.

The experiment uses an initial benign-flow calibration period to fit a Weibull model, removes that calibration prefix from the workload to avoid temporal leakage, and then replays the remaining trace through LINEAR. It records cumulative algorithm and adversary costs, compares the empirical adversary-to-algorithm cost ratio with the unscaled Theorem 1 proxy, and calculates Pearson correlation over every replay flow.

> This is an offline trace-replay experiment. It does not transmit packets or launch traffic against a live system.

## Main script

```text
trace_weibull_experiment.py
```

## Repository structure

```text
trace-driven-docker/
├── Dockerfile
├── .dockerignore
├── docker-compose.yml
├── requirements.txt
├── run_trace.ps1
├── trace_weibull_experiment.py
├── data/
│   └── <CIC-DDoS2019 CSV files>
└── results/
    └── <experiment outputs>
```

The input CSV files are not copied into the Docker image. They are mounted read-only from `data/`, while generated outputs are written to `results/`.

## Features

- Reads CIC-DDoS2019 flow CSV files using the `Timestamp` and `Label` columns.
- Resolves column names even when the CSV header contains surrounding whitespace.
- Sorts the trace chronologically using a stable timestamp/original-row ordering when needed.
- Uses the first **50 benign flows** by default to fit the initial Weibull estimator.
- Excludes the entire chronological prefix through the 50th benign flow from LINEAR replay.
- Fits a two-parameter Weibull distribution to positive benign inter-arrival times with location fixed at zero.
- Uses the fitted Weibull mean as the fixed LINEAR iteration length.
- Replays all remaining benign and malicious flows chronologically.
- Assigns prices without using the current flow label.
- Assumes every replayed flow pays the current LINEAR price and is serviced.
- Calculates cumulative algorithm cost `A`, adversary cost `B`, and empirical ratio `B/A`.
- Evaluates the raw, unscaled constant-gamma Theorem 1 proxy.
- Calculates Pearson correlation from the empirical and theoretical ratios at **every replay flow**.
- Saves plot/checkpoint values every **500 replay flows** by default.
- Produces both linear-y-axis and logarithmic-y-axis plots using the same raw ratio values.
- Supports full-file and chunked CSV parsing for large traces.
- Can optionally save the complete per-flow pricing trace.

## Docker requirements

For Windows, install:

- Docker Desktop
- Linux container support in Docker Desktop

A local Python installation is not required when using Docker.

The image uses Python 3.12 and installs the following pinned dependencies:

```text
numpy==2.3.5
pandas==2.2.3
scipy==1.17.0
matplotlib==3.10.8
```

## Build the Docker image

Open PowerShell in the repository directory and run:

```powershell
docker build -t ddos-trace-experiment:latest .
```

Rebuild the image whenever `trace_weibull_experiment.py`, `requirements.txt`, or the `Dockerfile` changes.

## Quick start on Windows

Place the dataset in the `data` directory. For example:

```text
data/DrDoS_MSSQL.csv
```

Then run:

```powershell
.\run_trace.ps1 `
  -Dataset "DrDoS_MSSQL.csv" `
  -RunName "MSSQL"
```

The results will be written to:

```text
results/MSSQL/
```

The PowerShell helper uses the current experiment defaults:

```text
checkpoint size        = 500 replay flows
estimator good flows   = 50 benign flows
read mode              = auto
chunk threshold        = 3078 MiB
chunk size             = 250000 rows
```

## Running with the PowerShell helper

The general form is:

```powershell
.\run_trace.ps1 `
  -Dataset "<dataset.csv>" `
  -RunName "<run-name>"
```

Example with all main options specified explicitly:

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

To save the complete per-flow pricing trace:

```powershell
.\run_trace.ps1 `
  -Dataset "DrDoS_MSSQL.csv" `
  -RunName "MSSQL" `
  -SaveFlowTrace
```

### PowerShell helper parameters

| Parameter | Description | Default |
|---|---|---:|
| `-Dataset` | CSV filename inside the `data/` directory | required |
| `-RunName` | Subdirectory created under `results/` | required |
| `-CheckpointSize` | Number of replay flows between plotted/saved checkpoints | `500` |
| `-EstimatorGoodFlows` | Number of earliest benign flows used for estimator calibration | `50` |
| `-ReadMode` | CSV loading mode: `auto`, `full`, or `chunked` | `auto` |
| `-ChunkThresholdMB` | File-size threshold for automatic chunked parsing | `3078` |
| `-ChunkSize` | Number of rows per CSV parsing chunk | `250000` |
| `-SaveFlowTrace` | Save one output row per replay flow | off |

## Running directly with Docker

The PowerShell helper is optional. The same experiment can be started directly with `docker run`:

```powershell
docker run --rm --init `
  --mount "type=bind,source=$((Resolve-Path .\data).Path),target=/data,readonly" `
  --mount "type=bind,source=$((Resolve-Path .\results).Path),target=/results" `
  ddos-trace-experiment:latest `
  /data/DrDoS_MSSQL.csv `
  --output-dir /results/MSSQL `
  --checkpoint-size 500 `
  --estimator-good-flows 50 `
  --read-mode auto `
  --chunk-threshold-mb 3078 `
  --chunk-size 250000
```

The input mount is read-only:

```text
/data
```

The output mount is writable:

```text
/results
```

Containerization changes only the execution environment. It does not change the timestamps, calibration procedure, Weibull fitting, LINEAR iteration boundaries, pricing, costs, Pearson calculation, or plotting checkpoints.

## Running with Docker Compose

Set the dataset filename and run name in PowerShell:

```powershell
$env:DATASET="DrDoS_MSSQL.csv"
$env:RUN_NAME="MSSQL"
docker compose run --rm trace-experiment
```

Optional Compose overrides supported by the current `docker-compose.yml` are:

```powershell
$env:CHECKPOINT_SIZE="500"
$env:ESTIMATOR_GOOD_FLOWS="50"
$env:READ_MODE="auto"
docker compose run --rm trace-experiment
```

The Compose service mounts:

```text
./data    -> /data     (read-only)
./results -> /results
```

## Command-line options

The Python script can also be run directly outside Docker if the required Python packages are installed.

Display all available options with:

```bash
python trace_weibull_experiment.py --help
```

The main options are:

| Option | Description | Default |
|---|---|---:|
| positional `csv_file` | Path to one CIC-DDoS2019 CSV file | required |
| `--output-dir` | Directory for CSV, JSON, and PNG outputs | `linear_weibull_results` |
| `--benign-label` | Label treated as benign | `BENIGN` |
| `--timestamp-column` | Timestamp column after whitespace stripping | `Timestamp` |
| `--label-column` | Label column after whitespace stripping | `Label` |
| `--checkpoint-size` | Plot/save every N replay flows | `500` |
| `--estimator-good-flows` | Earliest benign flows used for Weibull calibration | `50` |
| `--save-flow-trace` | Save one row per replay flow | off |
| `--read-mode` | `auto`, `full`, or `chunked` | `auto` |
| `--chunk-threshold-mb` | Automatic chunking threshold in MiB | `3078` |
| `--chunk-size` | Rows per parsing chunk | `250000` |

## Experiment procedure

### 1. Load and order the trace

Only the timestamp and label columns are required from the input CSV. Invalid timestamps are removed, labels are normalized, and each flow is classified as benign or malicious for later accounting.

If the input timestamps are already chronological, the existing order is retained. Otherwise, the script performs a stable sort by timestamp and original row number.

### 2. Initial estimator calibration

The earliest `--estimator-good-flows` benign flows are used for calibration. The default is:

```text
50 benign flows
```

The calibration prefix ends at the 50th benign flow. Every flow in that prefix is excluded from the LINEAR replay, including malicious flows that occur before the calibration endpoint.

This prevents replaying traffic that occurred before information used to construct the estimator was available.

### 3. Weibull fitting

Let the positive benign inter-arrival times in the calibration sample be `X`.

The script fits a two-parameter Weibull model by maximum likelihood with the location fixed at zero:

```text
X ~ Weibull(k, lambda)
```

The fitted mean is:

```text
E[X] = lambda * Gamma(1 + 1/k)
```

and the estimated benign arrival rate is:

```text
rho = 1 / E[X]
```

The fitted estimator is fixed after calibration and is not updated during replay.

### 4. LINEAR iterations

The estimator rule is:

```text
g_hat(I) = rho * length(I)
```

An iteration resets when:

```text
g_hat(I) >= 1
```

Therefore the fixed iteration length is:

```text
iteration_length = 1/rho = E[X]
```

Replay iterations are anchored to the timestamp of the first post-calibration replay flow.

### 5. LINEAR pricing

Within each iteration, LINEAR uses:

```text
PRICE = s + 1
```

where `s` is the number of jobs already serviced in the current iteration.

Therefore the first job in an iteration pays 1, the second pays 2, and so on.

The label of the current flow is not used to determine its price. The experiment assumes that every flow pays the current price and is serviced.

### 6. Cost calculation

For every replayed flow:

```text
B = cumulative fees paid by malicious flows
```

and

```text
A = cumulative fees paid by benign flows
    + cumulative server service cost
```

The normalized server service cost is 1 per serviced flow.

The empirical adversary-to-algorithm cost ratio is:

```text
Empirical ratio = B / A
```

### 7. Theoretical comparison

The experiment uses the raw constant-gamma Theorem 1 shape proxy:

```text
Theoretical ratio = B / (sqrt(B * (g + 1)) + (g + 1))
```

where `g` is the cumulative number of benign replay flows at that point.

No endpoint scaling, min-max normalization, or RMSE calculation is applied.

### 8. Pearson correlation

Pearson correlation is calculated from the empirical and theoretical ratio values at corresponding replay flows.

Conceptually, the two sequences are:

```text
Empirical:   E1, E2, E3, ..., EN
Theoretical: T1, T2, T3, ..., TN
```

and the experiment calculates:

```text
r = corr([E1, E2, ..., EN], [T1, T2, ..., TN])
```

Pearson uses **every replay flow**, not only the flows selected for plotting.

Flow number and inter-arrival time are not the two variables being correlated. They are used elsewhere in the experiment, but Pearson is calculated directly from the paired empirical and theoretical ratio values.

### 9. Plot checkpoints

The default checkpoint interval is:

```text
500 replay flows
```

Therefore plot/checkpoint values are recorded at:

```text
500, 1000, 1500, 2000, ...
```

The final replay flow is also included when the total replay size is not an exact multiple of 500.

The Pearson value displayed in the plot legend is still calculated using every replay flow.

## Output files

Each run creates the selected output directory.

For example:

```text
results/MSSQL/
```

### `experiment_summary.csv`

One-row summary of the experiment, including calibration diagnostics, Weibull parameters, replay counts, final costs, final ratios, and Pearson correlation.

Important fields include:

- number of calibration flows excluded;
- number of calibration benign and malicious flows;
- replay flow counts;
- fitted Weibull shape and scale;
- fitted mean inter-arrival time;
- estimated benign arrival rate;
- occupied LINEAR iterations;
- maximum LINEAR price;
- final adversary cost `B`;
- final algorithm cost `A`;
- final empirical `B/A`;
- final theoretical proxy;
- all-flow Pearson correlation;
- number of replay-flow pairs used for Pearson.

### `experiment_summary.json`

Contains the same run-level summary information in JSON format.

### `checkpoint_costs.csv`

Contains cumulative measurements at every plotting checkpoint.

Important columns include:

| Column | Description |
|---|---|
| `number_of_jobs` | Cumulative replay flows at the checkpoint |
| `benign_jobs` | Cumulative benign replay flows |
| `malicious_jobs` | Cumulative malicious replay flows |
| `honest_client_fees` | Cumulative fees paid by benign flows |
| `adversary_cost_B` | Cumulative adversary cost `B` |
| `server_service_cost` | Cumulative normalized service cost |
| `algorithm_cost_A` | Cumulative algorithm cost `A` |
| `adversary_over_algorithm_B_over_A` | Raw empirical `B/A` |
| `current_iteration_id` | Current estimator-defined iteration |
| `current_price` | Current LINEAR price |
| `theorem1_proxy` | Raw, unscaled theoretical ratio proxy |

### `iteration_summary.csv`

Contains one row for each occupied LINEAR iteration.

Important fields include:

| Column | Description |
|---|---|
| `iteration_id` | Zero-based iteration identifier |
| `iteration_start` | Timestamp of the first observed flow in the occupied iteration |
| `iteration_last_flow` | Timestamp of the last observed flow in the iteration |
| `total_jobs` | Total replay flows in the iteration |
| `benign_jobs` | Benign replay flows in the iteration |
| `malicious_jobs` | Malicious replay flows in the iteration |
| `maximum_price` | Highest LINEAR price reached in the iteration |
| `honest_client_fees` | Benign fees paid in the iteration |
| `adversary_cost_B` | Malicious fees paid in the iteration |
| `server_service_cost` | Service cost for the iteration |
| `algorithm_cost_A` | Algorithm cost for the iteration |
| `B_over_A` | Iteration-level adversary-to-algorithm ratio |

### `adversary_over_algorithm_vs_jobs_linear.png`

Plots the raw empirical `B/A` curve and raw theoretical proxy using a linear y-axis.

The legend also displays the Pearson correlation calculated over all replay flows.

### `adversary_over_algorithm_vs_jobs_log.png`

Plots the same raw empirical and theoretical values using a logarithmic y-axis.

The logarithmic version changes only the display scale; it does not transform the data used for Pearson correlation.

### `flow_pricing_trace.csv`

Generated only when `--save-flow-trace` or `-SaveFlowTrace` is enabled.

It contains one row per replay flow with the assigned iteration, LINEAR price, per-flow fee contribution, and cumulative costs. Because this file can be very large, it is disabled by default.

## Large CSV files and memory usage

The script supports three input modes:

```text
auto
full
chunked
```

In `auto` mode, the script checks the CSV file size. Files at or above the configured threshold are parsed in chunks; smaller files are loaded normally.

The current defaults are:

```text
chunk threshold = 3078 MiB
chunk size      = 250000 rows
```

Chunking reduces the peak memory required during CSV parsing. However, after parsing, the current implementation still combines the required timestamp/label information into an in-memory trace for sorting and replay. Very large traces therefore still require sufficient Docker memory.

For large CIC-DDoS2019 files, make sure Docker Desktop has enough memory available.

## Reproducibility

The trace-driven experiment contains no random traffic generation. Given the same:

- input CSV;
- Python experiment code;
- dependency versions;
- benign label;
- estimator calibration size;
- checkpoint size;
- parsing settings;

it should produce the same chronological replay, Weibull fit, LINEAR pricing, costs, theoretical proxy, Pearson correlation, and plot values.

Docker pins the Python package versions so that the software environment can be reproduced on another machine.

## Methodological notes

- Calibration uses only the earliest benign flows in chronological order.
- The entire prefix through the final calibration flow is removed from the workload to prevent temporal leakage.
- The Weibull estimator is fitted once and remains fixed throughout the replay.
- Only positive benign inter-arrival times are used for Weibull fitting.
- LINEAR pricing is label-blind; labels are used only after pricing for cost accounting.
- Every replay flow is assumed to pay the current LINEAR price and receive service.
- The server service cost is normalized to 1 per serviced flow.
- Pearson correlation is calculated using raw empirical and raw theoretical ratios at every replay flow.
- Plot sampling is independent of Pearson calculation: the plots use every 500th replay flow by default, while Pearson uses every replay flow.
- The theoretical curve is not endpoint-scaled or normalized.
- RMSE and normalized RMSE are not calculated in the current experiment.
- The linear and logarithmic plots use the same raw ratio values.
- Empty mathematical time intervals can exist between occupied iterations; the iteration IDs preserve those time gaps.
- `iteration_start` in `iteration_summary.csv` is the timestamp of the first observed replay flow in that occupied iteration, not the mathematical left boundary of the interval.
- CSV chunking changes only how large files are parsed; it does not change the experiment semantics.

## Expected console output

At completion, the script prints a summary similar to:

```text
Experiment completed
========================================================================
Rows used in replay:       ...
Calibration flows excluded: ...
Good flows:                ...
Bad flows:                 ...
Weibull shape k:           ...
Weibull scale lambda (s):  ...
Weibull mean E[X] (s):     ...
Estimated good rate rho:   ... flows/s
Occupied iterations:       ...
Maximum LINEAR price:      ...
Final adversary cost B:    ...
Final algorithm cost A:    ...
Final B/A:                 ...
Final Theorem 1 proxy:     ...
Pearson correlation r:     ...
Pearson comparison flows:  ...
Outputs:                   /results/<run-name>
```

## Example datasets

The same container can be used for different CIC-DDoS2019 traces by changing only the input filename and run name. For example:

```powershell
.\run_trace.ps1 -Dataset "DrDoS_MSSQL.csv" -RunName "MSSQL"
```

```powershell
.\run_trace.ps1 -Dataset "DrDoS_SSDP.csv" -RunName "SSDP"
```

```powershell
.\run_trace.ps1 -Dataset "DrDoS_UDP.csv" -RunName "UDP"
```

Each run writes to its own output directory under `results/`.
