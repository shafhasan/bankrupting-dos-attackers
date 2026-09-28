# Experiment 3: Client–Server Evaluation of LINEAR and LINEAR-POWER

This directory contains the Dockerized implementation used for **Experiment 3** of the resource-competitive DDoS evaluation. The experiment replays CIC-DDoS2019 flow records through a localhost client–server system and evaluates both **LINEAR** and **LINEAR-POWER** under either a fixed or sliding-window good-traffic estimator.

The Docker image contains the experiment code and Python dependencies. CIC-DDoS2019 CSV files are **not** copied into the image; they are mounted read-only at runtime. Results are written to a separate mounted directory.

## Experiment overview

For each CIC-DDoS2019 trace, the implementation:

1. orders the trace chronologically;
2. uses the earliest **50 benign flows** to initialize a two-parameter Weibull estimator;
3. treats the entire chronological prefix through and including the 50th benign flow as calibration traffic and excludes that prefix from the measured workload;
4. resets the pricing/iteration state before evaluation;
5. replays the remaining flows through both LINEAR and LINEAR-POWER; and
6. computes the empirical adversary-to-algorithm cost ratio and the corresponding raw theoretical trend.

Two estimator configurations are supported:

- **Fixed estimator:** the Weibull estimate obtained during calibration is used for the remainder of the trace.
- **Sliding-window estimator:** the estimator begins from the calibration samples, maintains the most recent **50 positive benign inter-arrival times**, and refits after every **10 new valid positive benign inter-arrival samples**. A newly estimated iteration length is adopted at an iteration boundary.

Dataset labels are used by the estimator and experimental controller to identify benign observations, instantiate the prescribed good-client/adversarial behavior, and compute costs. **Labels are not provided to the pricing server and do not directly determine the server price.**

## Requirements

- Docker Desktop or Docker Engine
- Docker Compose plugin if using `docker compose`
- CIC-DDoS2019 flow-level CSV files
- Sufficient disk space for the dataset and generated results

No GPU is required.

## Repository files

The main components are:

- `Dockerfile` — builds the experiment image.
- `docker-compose.yml` — convenience services for fixed and sliding-window runs.
- `run_fixed_calibration.py` — runs both LINEAR and LINEAR-POWER with the fixed estimator.
- `run_sliding_window.py` — runs both LINEAR and LINEAR-POWER with the sliding-window estimator.
- `run_experiment.py` — common experiment runner.
- `trace_controller.py` — reads the trace, performs calibration/evaluation splitting, replays flows, schedules retries, and maintains experiment-side accounting.
- `server.py` — pricing server implementing LINEAR and LINEAR-POWER.
- `estimator_service.py` — Weibull estimator service.
- `plot_b_over_a.py` — produces linear- and log-scale empirical/theoretical plots.
- `requirements.txt` — Python dependencies used inside the container.

## Input CSV format

The replay expects the CIC-DDoS2019-style columns:

```text
Source IP
Source Port
Destination IP
Destination Port
Protocol
Timestamp
Label
```

`BENIGN` is treated as the good-flow label. Other labeled flows are treated as attack flows.

All labeled protocol values are retained. Original protocol `17` is replayed over the UDP socket and protocol `6` over the TCP socket. Other protocol values retain their original metadata and are carried over the TCP replay transport.

## Build the Docker image

From the `experiment-3` directory:

```bash
docker build --no-cache -t ddos-exp3:latest .
```

The image is based on Python 3.12 and installs the dependencies in `requirements.txt`.

## Prepare data and result directories

A convenient local layout is:

```text
experiment-3/
├── data/
│   └── DrDoS_MSSQL.csv
├── results/
├── Dockerfile
├── docker-compose.yml
└── ...
```

Create the directories if needed:

```bash
mkdir -p data results
```

Place the CIC-DDoS2019 CSV file in `data/`.

## Run with Docker

### Fixed estimator

```bash
docker run --rm --init \
  --name exp3-mssql-fixed \
  -v "$PWD/data:/data:ro" \
  -v "$PWD/results:/results" \
  ddos-exp3:latest \
  python run_fixed_calibration.py \
    --csv /data/DrDoS_MSSQL.csv \
    --output-dir /results/DrDoS_MSSQL_fixed \
    --speedup 0 \
    --retry-delay 0.01 \
    --good-flow-timeout 60 \
    --socket-timeout 10 \
    --max-attempts 64
```

### Sliding-window estimator

```bash
docker run --rm --init \
  --name exp3-mssql-sliding \
  -v "$PWD/data:/data:ro" \
  -v "$PWD/results:/results" \
  ddos-exp3:latest \
  python run_sliding_window.py \
    --csv /data/DrDoS_MSSQL.csv \
    --output-dir /results/DrDoS_MSSQL_sliding \
    --speedup 0 \
    --retry-delay 0.01 \
    --good-flow-timeout 60 \
    --socket-timeout 10 \
    --max-attempts 64
```

No host ports need to be published. The runner starts the server and estimator as subprocesses inside the same container and communicates over container-localhost sockets.

`--init` is recommended because the experiment runner manages child processes.

## Run with Docker Compose

The supplied `docker-compose.yml` defines `fixed` and `sliding` services and mounts:

```text
./data    -> /data     (read-only)
./results -> /results  (read/write)
```

### macOS / Linux

Fixed estimator:

```bash
DATASET=DrDoS_MSSQL.csv \
RUN_NAME=DrDoS_MSSQL_fixed \
docker compose run --rm fixed
```

Sliding-window estimator:

```bash
DATASET=DrDoS_MSSQL.csv \
RUN_NAME=DrDoS_MSSQL_sliding \
docker compose run --rm sliding
```

### Windows PowerShell

Fixed estimator:

```powershell
$env:DATASET="DrDoS_MSSQL.csv"
$env:RUN_NAME="DrDoS_MSSQL_fixed"
docker compose run --rm fixed
```

Sliding-window estimator:

```powershell
$env:DATASET="DrDoS_MSSQL.csv"
$env:RUN_NAME="DrDoS_MSSQL_sliding"
docker compose run --rm sliding
```

## Important parameters

The paper configuration uses:

| Parameter | Default / paper value | Meaning |
|---|---:|---|
| Calibration good flows | 50 | Earliest benign flows used for the initial Weibull fit |
| Sliding-window size | 50 | Most recent positive benign IAT samples retained |
| Refit interval | 10 | New valid positive benign IAT samples required before a sliding refit |
| `--retry-delay` | 0.01 s | Logical trace-time delay before a rejected LINEAR-POWER good flow retries |
| `--good-flow-timeout` | 60 s | Maximum logical trace time allowed for a good flow |
| `--max-attempts` | 64 | Maximum attempts for a LINEAR-POWER good flow |
| `--socket-timeout` | 10 s | Wall-clock timeout for a local socket request |
| `--speedup` | 0 | Removes wall-clock pacing while preserving logical trace timing/order |
| CSV chunk size | 100000 | Rows processed per input chunk |
| Plot checkpoint | 500 flows | Frequency of stored points used for plotting |

The calibration count and sliding-window size are fixed at 50 in the current experiment configuration. Deprecated command-line options retained for backward compatibility do not change these values.

## LINEAR behavior

LINEAR is used as the zero-latency baseline. Its price within an iteration is

```text
1, 2, 3, 4, ...
```

Each original flow is processed once at the current server price. LINEAR does not use the rejection/retry mechanism used by LINEAR-POWER.

## LINEAR-POWER behavior

LINEAR-POWER uses the power-of-two price rule

```text
1, 2, 2, 4, 4, 4, 4, 8, ...
```

Good flows initially submit fee `1`. If a fee is insufficient when the request reaches the server, the flow is rejected, receives the current price, and is scheduled to retry after `--retry-delay` logical trace seconds.

Original dataset arrivals are processed before retries when they have exactly the same logical timestamp. Retries may therefore observe a different price and may cross iteration boundaries.

The experiment uses an informed minimum-fee adversary for LINEAR-POWER: before an attack flow is submitted, the controller obtains the current global price and attaches that amount. The pricing server itself still does not receive the flow label.

## Communication-delay proxy and Theorem 2 `M`

For LINEAR-POWER, the configured logical retry delay is used as the experimental proxy for the theoretical communication-delay parameter:

```text
Delta = retry-delay = 0.01 s
```

By default, the implementation computes `M` automatically from the post-calibration original benign arrivals as the maximum number of newly generated good flows in any interval of length `Delta`. Retries are excluded because they are not newly generated jobs.

A manual value can be supplied with `--theorem2-M`, but the paper experiments use the automatically derived trace-specific value.

## Cost accounting and theoretical comparison

For each serviced original flow, the experiment updates cumulative algorithm cost `A` and adversary cost `B`.

- The server incurs one unit of service cost per serviced flow.
- For a good flow, the final accepted fee contributes to the good-client component of `A`.
- Rejected good-flow fees are not charged again.
- All submitted attack-flow fees contribute to `B`.

The empirical curve is the cumulative adversary-to-algorithm ratio:

```text
B / A
```

The theoretical LINEAR and LINEAR-POWER curves are evaluated directly from the corresponding theorem-based expressions. The current implementation uses:

- **no final-point scaling**;
- **no normalization**; and
- **no RMSE calculation**.

Pearson correlation is calculated using **all valid evaluation-flow points** from the raw empirical and theoretical sequences. To keep output size manageable, only every 500th flow point, plus the final point, is retained for plotting.

Both linear-scale and logarithmic-scale plots are generated.

## Low-memory / low-I/O processing

The current implementation is designed for large CIC-DDoS2019 CSV files.

It reads only the columns needed by the experiment and processes the input in bounded chunks rather than loading the complete CSV into a single DataFrame. The default is:

```text
--csv-chunk-size 100000
```

If memory is constrained, reduce the chunk size, for example:

```bash
--csv-chunk-size 50000
```

Changing the chunk size changes only I/O and memory usage; it does not change the logical experiment.

The trace is processed in `(parsed timestamp, flow UID)` order. If the input file is already ordered, it is streamed directly. If it is not ordered, the controller performs a bounded-memory external merge sort using temporary files and deletes those temporary files after the run.

The low-I/O implementation avoids large per-flow intermediate CSVs such as `server_jobs.csv`, `evaluation_ground_truth.csv`, `controller_replay_log.csv`, and `priced_flows_with_ground_truth.csv` in the final results path.

## Output structure

Each fixed/sliding run evaluates both algorithms and creates separate subdirectories:

```text
results/DrDoS_MSSQL_fixed/
├── linear/
│   ├── evaluation_summary.json
│   ├── theorem_proxy_metadata.json
│   ├── selection_summary.json
│   ├── good_flow_completion_metrics.csv
│   ├── B_over_A_plot_points.csv
│   ├── B_over_A_linear_scale.png
│   └── B_over_A_log_scale.png
└── linear_power/
    ├── evaluation_summary.json
    ├── theorem_proxy_metadata.json
    ├── selection_summary.json
    ├── good_flow_completion_metrics.csv
    ├── B_over_A_plot_points.csv
    ├── B_over_A_linear_scale.png
    └── B_over_A_log_scale.png
```

The most useful files are:

- `evaluation_summary.json` — final `A`, `B`, `B/A`, Pearson correlation, and run-level statistics.
- `theorem_proxy_metadata.json` — theoretical-curve metadata, Pearson basis, and LINEAR-POWER `M`/delay-proxy information.
- `selection_summary.json` — calibration cutoff, evaluation counts, estimator initialization, ordering/input information, and experiment settings.
- `good_flow_completion_metrics.csv` — per-good-flow attempts, rejections, accepted fee, logical completion latency, and timeout status.
- `B_over_A_plot_points.csv` — compact every-500-flow series used to generate the figures.
- `B_over_A_linear_scale.png` and `B_over_A_log_scale.png` — empirical/theoretical comparison plots.

## Running multiple datasets

On macOS/Linux, the following example runs the fixed estimator for every CSV in `data/`:

```bash
for file in data/*.csv; do
  name="$(basename "$file" .csv)"

  docker run --rm --init \
    -v "$PWD/data:/data:ro" \
    -v "$PWD/results:/results" \
    ddos-exp3:latest \
    python run_fixed_calibration.py \
      --csv "/data/$(basename "$file")" \
      --output-dir "/results/${name}_fixed" \
      --speedup 0 \
      --retry-delay 0.01 \
      --good-flow-timeout 60 \
      --socket-timeout 10 \
      --max-attempts 64
done
```

Replace `run_fixed_calibration.py` and `_fixed` with `run_sliding_window.py` and `_sliding` to run the sliding-window configuration.

## Notes on reproducibility

- Dataset files are mounted read-only and are never baked into the Docker image.
- The evaluation uses logical trace time for iteration evolution and retry scheduling; wall-clock localhost timing is not used as the experimental network-delay measurement.
- The client and server run inside the same container. This is a controlled client–server experiment, not a wide-area network testbed.
- All source IPs and all labeled flows remaining after the calibration prefix are included.
- One global pricing state is shared across all clients, destination IPs, ports, and protocols.

## Troubleshooting

### Container exits with code 137 / `SIGKILL`

This usually indicates that Docker ran out of memory. Increase the memory available to Docker Desktop or reduce the CSV chunk size:

```bash
--csv-chunk-size 50000
```

### Input CSV is not timestamp sorted

No manual full-file sort is required. The current controller detects unsorted input and uses a bounded-memory external merge sort. Temporary sort files are removed after the run.

### Output directory is empty after the container exits

Make sure the host results directory exists and is mounted to `/results`:

```bash
-v "$PWD/results:/results"
```

Also make sure `--output-dir` points somewhere under `/results` inside the container.

### Docker Compose cannot find the dataset

Confirm that the file exists under `./data/` and that `DATASET` contains only the filename relative to that directory, for example:

```bash
DATASET=DrDoS_MSSQL.csv RUN_NAME=DrDoS_MSSQL_fixed docker compose run --rm fixed
```

## Native Python execution

Docker is the recommended reproducible path. If running directly with Python instead, install:

```bash
python -m pip install -r requirements.txt
```

Then use the same entry points:

```bash
python run_fixed_calibration.py \
  --csv /path/to/trace.csv \
  --output-dir results/fixed \
  --speedup 0 \
  --retry-delay 0.01 \
  --good-flow-timeout 60 \
  --socket-timeout 10 \
  --max-attempts 64
```

or

```bash
python run_sliding_window.py \
  --csv /path/to/trace.csv \
  --output-dir results/sliding \
  --speedup 0 \
  --retry-delay 0.01 \
  --good-flow-timeout 60 \
  --socket-timeout 10 \
  --max-attempts 64
```

## Scope

This implementation evaluates the pricing behavior of LINEAR and LINEAR-POWER using replayed flow-level traffic. The service fee is represented as an abstract resource cost; the experiment does not instantiate a concrete computational, memory, or bandwidth puzzle. The localhost setup also does not model real wide-area packet loss, jitter, congestion, or physical network latency.
