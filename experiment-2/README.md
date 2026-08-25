# Trace-Driven LINEAR Experiment with Weibull Estimator

This repository contains a trace-driven implementation of the **LINEAR pricing algorithm** evaluated on CIC-DDoS2019 flow traces. The experiment fits a Weibull model to benign-flow inter-arrival times, uses the fitted model to define estimator-driven iterations, replays all flows chronologically through LINEAR, and compares the empirical adversary-to-algorithm cost ratio against a scaled theoretical trend.

## Overview

The experiment follows this pipeline:

1. Read a CIC-DDoS2019 CSV trace.
2. Sort flows chronologically by timestamp.
3. Use all `BENIGN` flows to compute benign inter-arrival times.
4. Fit a two-parameter Weibull distribution with location fixed at zero.
5. Compute the fitted benign inter-arrival mean

   \[
   E[X] = \lambda \Gamma\left(1 + \frac{1}{k}\right)
   \]

   and the estimated benign arrival rate

   \[
   \rho = \frac{1}{E[X]}.
   \]

6. Use fixed estimator-defined iterations of length `E[X]`.
7. Replay **all flows** through LINEAR in chronological order.
8. Assign the LINEAR price inside each iteration as

   \[
   \text{PRICE} = s + 1,
   \]

   where `s` is the number of already-serviced jobs in that iteration.
9. Assume every flow pays the required price and is serviced.
10. Use labels only after pricing to compute empirical costs.
11. Record cumulative results at fixed flow-count checkpoints.
12. Compare the empirical cost ratio with a scaled theoretical curve.

## Cost Model

For the first `N` total flows in the trace:

- `B_N` is the cumulative fee paid by malicious flows.
- `A_N` is the cumulative benign-client fee plus the normalized server service cost.
- Each serviced flow contributes a server cost of `1`.

Therefore,

\[
A_N = \text{benign fees through }N + N,
\]

and

\[
B_N = \text{malicious fees through }N.
\]

The empirical curve is

\[
\frac{B_N}{A_N}.
\]

The x-axis always represents the **cumulative number of total flows processed**, not the number of benign flows.

## Weibull Estimator and Iterations

The estimator is based on the fitted benign inter-arrival distribution.

If the fitted Weibull mean is `E[X]`, the mathematical iteration boundaries are

\[
t_0,\; t_0 + E[X],\; t_0 + 2E[X],\; \ldots
\]

where `t_0` is the timestamp of the first flow in the trace.

A flow with timestamp `t` is assigned to

\[
\left\lfloor \frac{t-t_0}{E[X]} \right\rfloor.
\]

This means:

- iterations start at mathematical boundaries determined by the estimator;
- they do **not** start at the timestamp of the first observed flow in an occupied interval;
- empty mathematical iterations are preserved implicitly;
- the first flow in a new iteration receives price `1`.

## Theoretical Comparison

The experiment compares the empirical curve with a constant-gamma Theorem 1 proxy:

\[
T_N =
\frac{B_N}
{\sqrt{B_N(g_N+1)} + (g_N+1)},
\]

where `g_N` is the cumulative number of benign flows among the first `N` total flows.

A single global scale factor is chosen at the final valid checkpoint:

\[
c =
\frac{(B/A)_{\text{final}}}
{T_{\text{final}}}.
\]

The plotted theoretical curve is then

\[
cT_N.
\]

The final point therefore matches by construction. The purpose of the comparison is to study whether the **shape and asymptotic behavior** of the experimental curve follow the theoretical trend before the final point.

## Dataset Requirements

The script expects a CIC-DDoS2019-style CSV containing at least:

- `Timestamp`
- `Label`

Leading or trailing whitespace in column names is handled automatically.

The benign label defaults to:

```text
BENIGN
```

At least three benign flows are required so that benign inter-arrival times can be computed and a Weibull model can be fitted.

## Requirements

Recommended environment:

- Python 3.10+
- pandas
- NumPy
- SciPy
- Matplotlib

Install dependencies with:

```bash
pip install pandas numpy scipy matplotlib
```

## Running the Experiment

Basic usage:

```bash
python linear_weibull_experiment_theorem1.py "DrDoS_DNS.csv"
```

Specify an output directory:

```bash
python linear_weibull_experiment_theorem1.py "DrDoS_DNS.csv" \
    --output-dir "linear_weibull_results"
```

On Windows PowerShell:

```powershell
python linear_weibull_experiment_theorem1.py "DrDoS_DNS.csv" `
    --output-dir "linear_weibull_results"
```

## Command-Line Options

### `--output-dir`

Directory where result files and plots are written.

Default:

```text
linear_weibull_results
```

### `--benign-label`

Label treated as benign/good traffic.

Default:

```text
BENIGN
```

### `--timestamp-column`

Timestamp column name after surrounding whitespace is removed.

Default:

```text
Timestamp
```

### `--label-column`

Label column name after surrounding whitespace is removed.

Default:

```text
Label
```

### `--checkpoint-size`

Number of total flows between cumulative checkpoints.

Default:

```text
100
```

For example, the default records results at:

```text
100, 200, 300, ..., final flow
```

### `--save-flow-trace`

Save one output row per flow with iteration, price, and cumulative cost information.

Example:

```bash
python linear_weibull_experiment_theorem1.py "DrDoS_DNS.csv" \
    --save-flow-trace
```

This can create a very large CSV for multi-million-flow traces.

## Dynamic CSV Loading

The latest implementation can select the CSV loading strategy dynamically based on file size.

Default behavior:

- smaller CSV files are read normally in one operation;
- files at or above the configured size threshold use chunked loading;
- if a normal full-file read unexpectedly runs out of memory, automatic mode retries using chunks.

Default values:

```text
chunk threshold: 1024 MiB
chunk size:      250000 rows
```

### `--read-mode auto`

Automatically select full-file or chunked loading.

```bash
python linear_weibull_experiment_theorem1.py "TFTP.csv" \
    --read-mode auto
```

### `--read-mode full`

Force the original full-file loader.

```bash
python linear_weibull_experiment_theorem1.py "DrDoS_DNS.csv" \
    --read-mode full
```

### `--read-mode chunked`

Force chunked loading.

```bash
python linear_weibull_experiment_theorem1.py "TFTP.csv" \
    --read-mode chunked
```

### `--chunk-threshold-mb`

Change the file-size threshold used by automatic mode.

```bash
python linear_weibull_experiment_theorem1.py "TFTP.csv" \
    --chunk-threshold-mb 750
```

### `--chunk-size`

Change the number of rows read per chunk.

```bash
python linear_weibull_experiment_theorem1.py "TFTP.csv" \
    --chunk-size 100000
```

Chunked loading changes only how the CSV is read. It does not modify timestamps, flow order, Weibull parameters, iteration boundaries, LINEAR prices, or cost calculations.

## Output Files

The output directory contains the main experimental results.

### `experiment_summary.csv`

One-row summary containing values such as:

- number of flows used;
- benign and malicious flow counts;
- fitted Weibull shape and scale;
- fitted Weibull mean;
- estimated benign rate `rho`;
- occupied iteration count;
- maximum LINEAR price;
- final adversary cost `B`;
- final algorithm cost `A`;
- final `B/A`;
- theoretical scaling constant.

### `experiment_summary.json`

JSON version of the experiment summary.

### `checkpoint_costs.csv`

Cumulative results at each checkpoint, including:

- total flows processed;
- cumulative benign flows;
- cumulative malicious flows;
- honest-client fees;
- adversary cost `B`;
- server service cost;
- algorithm cost `A`;
- empirical `B/A`;
- current estimator iteration;
- current LINEAR price;
- raw theoretical proxy;
- theoretical scale constant;
- scaled theoretical proxy.

### `iteration_summary.csv`

Per-occupied-iteration summary containing values such as:

- iteration ID;
- first observed flow timestamp in that iteration;
- last observed flow timestamp;
- total jobs;
- benign and malicious job counts;
- maximum price;
- honest-client fees;
- adversary cost;
- algorithm cost;
- iteration-level `B/A`.

Note: the stored `iteration_start` value is the timestamp of the first observed flow in the occupied iteration, not the mathematical estimator boundary.

### `flow_pricing_trace.csv`

Created only with `--save-flow-trace`.

Contains per-flow fields such as:

- job number;
- original CSV row;
- timestamp;
- elapsed time;
- label / benign indicator;
- iteration ID;
- position within the iteration;
- LINEAR price;
- benign fee;
- adversary fee;
- cumulative costs;
- cumulative `B/A`.

## Generated Plots

The experiment saves both linear- and log-scale versions of the empirical-versus-theoretical comparison:

```text
adversary_over_algorithm_vs_jobs_linear.png
adversary_over_algorithm_vs_jobs_log.png
```

The plot settings include:

- figure size: `11 x 6` inches;
- empirical curve: solid line;
- theoretical curve: dashed line;
- line width: `1.4`;
- axis-label font size: `14`;
- tick-label font size: `14`;
- legend font size: `14`;
- axis-label color: `darkblue`;
- grid enabled for major and minor ticks;
- output resolution: `300 dpi`.

The log-scale graph is especially useful when the ratio changes by several orders of magnitude.

## Important Experimental Assumptions

### Offline estimator fitting

All benign labels in the trace are used before replay to fit the Weibull distribution. This is therefore an **offline full-trace estimator baseline**.

### Pricing does not inspect labels

Once the Weibull model has been fitted, LINEAR pricing is based only on:

- timestamp;
- estimator-defined iteration;
- position within the iteration.

The benign/malicious label does not affect the assigned price.

### Labels are used for evaluation

After pricing, labels are used to classify fees as either:

- honest-client cost; or
- adversary cost.

### Unlimited-budget assumption

Every flow is assumed to pay the current LINEAR price and receive service. The CIC-DDoS2019 CSV files do not contain client budgets or payment decisions, so this is an explicit simulation assumption.

### Server cost

Every serviced flow contributes a normalized server service cost of `1`.

## Interpreting the Theory Plots

The empirical curve is:

\[
B_N/A_N.
\]

The dashed curve is the scaled theoretical proxy.

Because both curves are cumulative:

- early parts of a trace can be highly variable;
- abrupt drops can appear when the relative growth of `A`, `B`, or `g` changes;
- later portions are typically smoother because individual events have less influence on already-large cumulative totals.

The most meaningful signs of theoretical alignment are:

- similar long-run slopes;
- similar major increases or decreases;
- similar plateau regions;
- decreasing separation as the trace grows.

The final overlap alone should not be treated as evidence of agreement because the scaling constant is selected at that final checkpoint.

## Reproducibility Notes

For a fixed CSV and environment, the experiment is deterministic because:

- flows are sorted by timestamp;
- original row order is used as a stable tie-breaker for equal timestamps;
- Weibull fitting uses the same benign inter-arrival sample;
- mathematical iteration boundaries are fixed once the Weibull mean is fitted;
- LINEAR pricing is deterministic within each iteration.

## Example Workflow

```bash
python linear_weibull_experiment_theorem1.py "DrDoS_MSSQL.csv" \
    --output-dir "results_mssql"

python linear_weibull_experiment_theorem1.py "DrDoS_UDP.csv" \
    --output-dir "results_udp"

python linear_weibull_experiment_theorem1.py "TFTP.csv" \
    --output-dir "results_tftp" \
    --read-mode auto
```

Each dataset is processed independently and produces its own summary tables and plots.

## Notes

This implementation is intended for trace-driven evaluation of LINEAR and the estimator behavior. It is not an online classifier and does not attempt to infer benign or malicious labels during pricing.
