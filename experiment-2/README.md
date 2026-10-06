# Trace-Driven LINEAR Experiment

This directory contains the trace-driven experiment used to evaluate the **LINEAR** pricing algorithm on CIC-DDoS2019 flow traces.

The experiment uses the first 50 benign flows as a one-time calibration sample for a Weibull estimator. To avoid temporal leakage, the entire chronological prefix through the 50th benign flow is removed from the workload before LINEAR replay begins. The fitted estimate is then kept fixed for the remaining trace.

> This is an offline trace-replay experiment. It does not send network traffic or launch a live attack.

## Repository structure

```text
experiment/
├── Dockerfile
├── .dockerignore
├── docker-compose.yml
├── requirements.txt
├── experiment-2.py
├── data/
└── results/
```

### Main files

- `experiment-2.py` — implements CSV loading, calibration, Weibull fitting, LINEAR replay, cost calculation, theoretical comparison, Pearson correlation, plotting, and result export.
- `Dockerfile` — builds the Python environment used by the experiment.
- `docker-compose.yml` — provides an alternative Docker Compose workflow.
- `requirements.txt` — lists the required Python packages.
- `data/` — place CIC-DDoS2019 CSV files here.
- `results/` — experiment outputs are written here.

## Requirements

Only Docker is required on the host machine.

For Windows, install:

- Docker Desktop
- PowerShell

The Python dependencies are installed inside the container.

## Build the Docker image

From the experiment directory, run:

```powershell
docker build -t ddos-trace-experiment:latest .
```

The image only needs to be rebuilt when the Python code, Dockerfile, or Python dependencies change.

## Dataset placement

Place any CIC-DDoS2019 CSV file that you want to evaluate inside the local `data` directory.

For example:

```text
data/
├── DrDoS_MSSQL.csv
├── DrDoS_SSDP.csv
├── DrDoS_UDP.csv
└── another_trace.csv
```

The experiment is not tied to a specific attack trace. Any compatible CSV file may be supplied as long as it contains the configured timestamp and label columns.

The dataset directory is mounted read-only inside the container, so the experiment cannot modify the original CSV.

## Running directly with Docker

General form:

```powershell
docker run --rm --init `
  --mount "type=bind,source=$((Resolve-Path .\data).Path),target=/data,readonly" `
  --mount "type=bind,source=$((Resolve-Path .\results).Path),target=/results" `
  ddos-trace-experiment:latest `
  /data/<dataset-file>.csv `
  --output-dir /results/<run-name>
```

Example:

```powershell
docker run --rm --init `
  --mount "type=bind,source=$((Resolve-Path .\data).Path),target=/data,readonly" `
  --mount "type=bind,source=$((Resolve-Path .\results).Path),target=/results" `
  ddos-trace-experiment:latest `
  /data/DrDoS_MSSQL.csv `
  --output-dir /results/MSSQL
```

## Running with Docker Compose

Set the dataset filename and choose a run name for the output directory:

```powershell
$env:DATASET="<dataset-file>.csv"
$env:RUN_NAME="<run-name>"
```

Example:

```powershell
$env:DATASET="DrDoS_SSDP.csv"
$env:RUN_NAME="SSDP"
```

Then run:

```powershell
docker compose run --rm trace-experiment
```

## Command-line options

Display the script options with:

```powershell
docker run --rm ddos-trace-experiment:latest --help
```

| Option | Description | Default |
|---|---|---:|
| `csv_file` | Path to the CIC-DDoS2019 CSV inside the container | Required |
| `--output-dir` | Output directory | `linear_weibull_results` |
| `--benign-label` | Label treated as benign | `BENIGN` |
| `--timestamp-column` | Timestamp column name | `Timestamp` |
| `--label-column` | Label column name | `Label` |
| `--checkpoint-size` | Plot/save one point every N replay flows | `500` |
| `--estimator-good-flows` | Number of earliest benign flows used for calibration | `50` |
| `--save-flow-trace` | Save one output row per replay flow | Off |
| `--read-mode` | CSV loading mode: `auto`, `full`, or `chunked` | `auto` |
| `--chunk-threshold-mb` | File-size threshold for automatic chunked loading | `3078` |
| `--chunk-size` | Rows per chunk in chunked mode | `250000` |

## Experiment procedure

### 1. Load and order the trace

The script reads the timestamp and label columns required by the experiment. Rows with invalid timestamps are removed.

If the trace is already chronological, the original order is retained. Otherwise, flows are stably sorted by timestamp and original row number.

### 2. Build the calibration prefix

The first 50 benign flows are selected chronologically.

The calibration prefix contains every flow from the beginning of the trace through the 50th benign flow, including malicious flows that occur in that interval.

The entire prefix is excluded from the workload after calibration. This prevents replaying flows that occurred before information used by the estimator became available.

```text
Original trace
|---------------- calibration prefix ----------------|------ replay ------|
                    50th benign flow
```

### 3. Fit the Weibull estimator

Positive inter-arrival times between the 50 calibration benign flows are used to fit a two-parameter Weibull distribution with location fixed at zero.

The fitted mean is:

```text
E[X] = lambda * Gamma(1 + 1/k)
```

where:

```text
k      = Weibull shape
lambda = Weibull scale
```

The estimated benign rate is:

```text
rho = 1 / E[X]
```

This estimate is fixed after calibration and is not updated during replay.

### 4. Define LINEAR iterations

The estimator rule is:

```text
g_hat(I) = rho * length(I)
```

An iteration ends when:

```text
g_hat(I) >= 1
```

Therefore the iteration length is:

```text
1 / rho = E[X]
```

The remaining trace is partitioned into fixed-duration mathematical iterations of width `E[X]`.

### 5. Apply LINEAR pricing

Within each iteration:

```text
PRICE = s + 1
```

where `s` is the number of already-serviced jobs in the current iteration.

The first flow in an iteration therefore pays 1, the second pays 2, and so on.

Pricing does not inspect the flow label. Every replayed flow is assumed to pay the current price and receive service.

### 6. Calculate cumulative costs

For every replay flow:

```text
B = cumulative fees paid by malicious flows
A = cumulative fees paid by benign flows + cumulative server service cost
```

The normalized service cost is 1 per serviced flow.

The empirical adversary-to-algorithm ratio is:

```text
B / A
```

### 7. Calculate the theoretical comparison

The experiment uses the constant-gamma Theorem 1 shape proxy:

```text
B / (sqrt(B * (g + 1)) + (g + 1))
```

where `g` is the cumulative number of benign replay flows.

### 8. Calculate Pearson correlation

Pearson correlation is calculated using every replay flow.

For replay flow `i`, the paired values are:

```text
empirical_i   = cumulative B/A at flow i
theoretical_i = raw theoretical proxy at flow i
```

Pearson therefore compares:

```text
(empirical_1, theoretical_1)
(empirical_2, theoretical_2)
...
(empirical_N, theoretical_N)
```

Flow number and inter-arrival time are not the variables being correlated.

The plots are sampled every 500 replay flows, but Pearson uses all replay flows.

## Plotting

Two plots are produced from the same raw empirical and theoretical values.

### Linear plot

```text
adversary_over_algorithm_vs_jobs_linear.png
```

Uses a linear y-axis.

### Logarithmic plot

```text
adversary_over_algorithm_vs_jobs_log.png
```

Uses a logarithmic y-axis.

The underlying values are unchanged; only the y-axis display scale differs.

Both plots show:

- empirical adversary-to-algorithm ratio;
- raw theoretical ratio proxy;
- Pearson correlation calculated from all replay flows.

## Output files

Each run creates the selected result directory.

### `experiment_summary.csv`

One-row experiment summary including:

- replay flow counts;
- calibration diagnostics;
- Weibull parameters;
- fitted mean inter-arrival time;
- estimated benign rate;
- LINEAR iteration information;
- final adversary cost;
- final algorithm cost;
- final empirical ratio;
- final theoretical proxy;
- Pearson correlation;
- number of replay flows used for Pearson.

### `experiment_summary.json`

JSON version of the experiment summary.

### `checkpoint_costs.csv`

Contains the plot/checkpoint values recorded every `--checkpoint-size` replay flows.

| Column | Description |
|---|---|
| `number_of_jobs` | Cumulative replay flows |
| `benign_jobs` | Cumulative benign replay flows |
| `malicious_jobs` | Cumulative malicious replay flows |
| `honest_client_fees` | Cumulative benign fees |
| `adversary_cost_B` | Cumulative adversary cost |
| `server_service_cost` | Cumulative service cost |
| `algorithm_cost_A` | Cumulative algorithm cost |
| `adversary_over_algorithm_B_over_A` | Empirical ratio |
| `theorem1_proxy` | Raw theoretical proxy |
| `current_iteration_id` | Current estimator iteration |
| `current_price` | Current LINEAR price |

### `iteration_summary.csv`

Contains one row per occupied LINEAR iteration, including:

- iteration ID;
- first and last observed flow timestamps;
- total jobs;
- benign jobs;
- malicious jobs;
- maximum price;
- benign fees;
- adversary cost;
- service cost;
- algorithm cost;
- iteration-level `B/A`.

### `flow_pricing_trace.csv`

Generated only when `--save-flow-trace` is used.

It contains one row per replay flow with pricing and cumulative cost information and can be large for multi-million-flow traces.

## Reproducibility

For a fixed CSV file and identical command-line options, the experiment is deterministic.

The estimator is fitted from the same first 50 benign flows, the same chronological calibration prefix is removed, and the same replay timestamps determine the same LINEAR iterations and prices.

Containerization keeps the Python dependency environment consistent across machines.

## Methodological notes

- The estimator is calibrated only once.
- Only the first 50 benign flows are used for estimator fitting by default.
- The entire prefix through the 50th benign flow is removed before replay.
- The fitted estimate remains fixed for the complete remaining workload.
- LINEAR pricing is label-blind.
- Labels are used only to determine whether a paid fee contributes to benign cost or adversary cost.
- Every replay flow is assumed to pay the current price and receive service.
- The empirical and theoretical curves are not scaled or normalized.
- Pearson correlation uses raw values at every replay flow.
- Plotting is performed every 500 replay flows by default.
- The theoretical curve is a shape proxy derived from the constant-gamma form of Theorem 1, not an exact equality for empirical `B/A`.
- Large traces may still require substantial memory after CSV loading because the replay state is stored in pandas data structures.
