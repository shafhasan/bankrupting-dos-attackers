# Synthetic Traffic Experiment for LINEAR

This repository contains a synthetic traffic experiment for evaluating the
**LINEAR** pricing algorithm under benign Poisson arrivals and multiple
malicious-traffic patterns. The experiment generates flow timestamps, merges
the benign and malicious flows chronologically, replays the combined trace
through LINEAR, and reports the cumulative algorithm and adversary costs.

> This is a trace-generation and replay experiment. It does not transmit
> network packets or launch traffic against a real system.

## Main script

```text
poisson_linear_experiment.py
```

## Features

- Generates benign flows using a homogeneous Poisson arrival process.
- Supports `constant`, `poisson`, and `burst` malicious traffic.
- Estimates the benign arrival rate from the generated benign trace.
- Replays all flows chronologically using label-blind LINEAR pricing.
- Records bad-only and completely empty iterations.
- Calculates the cumulative algorithm cost $A$, adversary cost $B$, and ratio
  $B/A$.
- Compares the empirical ratio with a scaled theoretical proxy.
- Produces CSV, JSON, and PNG outputs.
- Uses a configurable random seed for reproducibility.

## Requirements

- Python 3.9+
- NumPy
- SciPy
- Matplotlib

Install the dependencies with:

```bash
python -m pip install numpy scipy matplotlib
```

## Quick start

Run the script with its default configuration:

```bash
python poisson_linear_experiment.py
```

Display all available options:

```bash
python poisson_linear_experiment.py --help
```

## Reproducing the 100,000-flow constant-traffic experiment

The following command generates 100,000 flows, of which 20% are benign. The benign arrival rate is 15 flows per second, and malicious flows are spread uniformly across the trace.

```bash
python poisson_linear_experiment.py --total-flows 100000 --good-fraction 0.20 --good-rate 15 --bad-pattern constant --checkpoint-step 100 --seed 42 --output-dir results_100k_constant
```

With 20% benign and 80% malicious traffic, the experiment generates:

$$
N_g = 0.20(100000) = 20000
$$

benign flows and

$$
N_b = 100000 - 20000 = 80000
$$

malicious flows.


## Additional examples

### Burst traffic

This example assigns all malicious flows to bursts of 500 flows.

```bash
python poisson_linear_experiment.py --total-flows 100000 --good-fraction 0.20 --good-rate 15 --bad-pattern burst --burst-size 500 --seed 42 --output-dir results_100k_burst
```

### Longer runs

```bash
python poisson_linear_experiment.py --total-flows 500000 --good-fraction 0.20 --good-rate 15 --bad-pattern constant --seed 42 --output-dir results_500k_constant
```

```bash
python poisson_linear_experiment.py --total-flows 1000000 --good-fraction 0.20 --good-rate 15 --bad-pattern constant --seed 42 --output-dir results_1m_constant
```

## Command-line options

| Option | Description | Default |
|---|---|---:|
| `--total-flows` | Total number of benign and malicious flows | `1000000` |
| `--good-fraction` | Fraction of all flows labeled benign | `0.20` |
| `--good-rate` | True Poisson rate of benign flows in flows/s | `100.0` |
| `--bad-pattern` | Malicious pattern: `burst`, `poisson`, or `constant` | `constant` |
| `--burst-size` | Number of malicious flows assigned to each burst | `1000` |
| `--checkpoint-step` | Number of cumulative flows between recorded cost checkpoints | `100` |
| `--seed` | NumPy random seed | `42` |
| `--output-dir` | Directory in which results are written | `poisson_linear_results` |

## Traffic generation

### Number of benign and malicious flows

For a total of $N$ flows and benign fraction $f_g$, the code calculates:

$$
N_g = \operatorname{round}(Nf_g)
$$

and

$$
N_b = N-N_g.
$$

Use at least three total flows. The count adjustment then retains at least two
benign flows and at least one malicious flow, as required by the rate
estimator and experiment.

### Benign Poisson arrivals

Benign inter-arrival times are independent exponential random variables:

$$
X_i \sim \operatorname{Exponential}(\lambda_g).
$$

Their expected value is:

$$
E[X_i] = \frac{1}{\lambda_g}.
$$

The benign timestamps are cumulative sums of these inter-arrival times:

$$
t_i = \sum_{j=1}^{i}X_j.
$$

### Constant malicious traffic

For the `constant` pattern, the $N_b$ malicious timestamps are evenly spaced
over the duration $T$ of the generated benign trace. The implied malicious
rate is:

$$
\lambda_b = \frac{N_b}{T},
$$

and the deterministic spacing is:

$$
\Delta t_b = \frac{T}{N_b} = \frac{1}{\lambda_b}.
$$

`constant` therefore means evenly spaced malicious arrivals, not Poisson
malicious arrivals.

## Benign-rate estimation

After the complete benign trace is generated, the code estimates its rate from
all positive benign inter-arrival times:

$$
\widehat{\lambda}_g =
\frac{1}{\frac{1}{m}\sum_{i=1}^{m}X_i}.
$$

This estimate is calculated once and remains fixed throughout the LINEAR
replay. The current implementation therefore performs **offline calibration
followed by online-style sequential replay**.

## LINEAR iterations

The estimated iteration length is:

$$
L = \frac{1}{\widehat{\lambda}_g}.
$$

The earliest timestamp in the merged trace anchors the iteration grid. The
boundaries are:

$$
t_0,\ t_0+L,\ t_0+2L,\ldots
$$

Iterations use half-open intervals:

$$
[t_k,t_{k+1}).
$$

Consequently, a flow arriving exactly at $t_{k+1}$ belongs to the new
iteration. A `while` loop advances through every elapsed boundary, so gaps can
produce bad-only or completely empty iterations without shifting the future
boundary grid. Empty trailing intervals after the final flow are not added.

When $\widehat{\lambda}_g \approx \lambda_g$, a Poisson benign process contains
approximately one benign flow per iteration on average:

$$
E[G_k] = \lambda_gL
= \frac{\lambda_g}{\widehat{\lambda}_g}
\approx 1.
$$

The realized count is random, so an iteration may contain zero, one, or
multiple benign flows.

## LINEAR pricing and costs

Suppose $s$ flows have already been serviced in the current iteration. The
next flow receives price:

$$
p=s+1.
$$

The price depends only on the number of preceding flows in the iteration. The
good/bad label is not used when assigning the price.

The cumulative algorithm cost is:

$$
A = N_{\mathrm{serviced}}
+ \sum_{j\in\mathcal{G}}p_j,
$$

where the first term is one normalized service-cost unit for every serviced
flow and $\mathcal{G}$ is the set of benign flows.

The cumulative adversary cost is:

$$
B = \sum_{j\in\mathcal{B}}p_j,
$$

where $\mathcal{B}$ is the set of malicious flows.

The reported performance metric is:

$$
\frac{B}{A}.
$$

## Theorem 1 proxy

At each checkpoint, the script evaluates the unscaled proxy:

$$
R_{\mathrm{theory}} =
\frac{B}{\sqrt{B(g+1)}+(g+1)},
$$

where $g$ is the cumulative number of benign flows.

A single scaling constant $c$ is calculated from the final checkpoint:

$$
c =
\frac{(B/A)_{\mathrm{final}}}
{R_{\mathrm{theory,final}}}.
$$

The plotted theoretical curve is:

$$
cR_{\mathrm{theory}}.
$$

Because $c$ is calibrated at the final point, agreement between the empirical
and theoretical curves at that endpoint is guaranteed. Their behavior before
the endpoint is the meaningful shape comparison.

## Output files

Each run creates the selected output directory and writes the following files.

### `linear_checkpoints.csv`

Contains cumulative measurements recorded every `--checkpoint-step` flows:

| Column | Description |
|---|---|
| `flows` | Cumulative number of processed flows |
| `cumulative_good` | Cumulative number of benign flows |
| `algorithm_cost` | Cumulative algorithm cost $A$ |
| `adversary_cost` | Cumulative adversary cost $B$ |
| `cost_ratio` | Cumulative ratio $B/A$ |
| `theorem_raw` | Unscaled Theorem 1 proxy |
| `theorem_scaled` | Scaled Theorem 1 proxy |

### `iteration_composition.csv`

Contains one row per mathematical iteration:

| Column | Description |
|---|---|
| `iteration_number` | One-based iteration number |
| `iteration_start_time` | Mathematical start boundary |
| `iteration_end_time` | Mathematical end boundary |
| `good_jobs` | Benign flows in the iteration |
| `bad_jobs` | Malicious flows in the iteration |
| `total_jobs` | Total flows in the iteration |
| `contains_both` | Whether both flow types occur |
| `has_no_good` | Whether the iteration contains no benign flows |
| `is_empty` | Whether the iteration contains no flows |

### `summary.json`

Contains:

- The complete experiment configuration.
- Generated benign and malicious flow counts.
- Trace duration and true/estimated benign rates.
- Poisson diagnostic statistics.
- Final $A$, $B$, and $B/A$ values.
- The number and length of LINEAR iterations.
- Counts of mixed, bad-only, no-bad, no-good, and empty iterations.
- The final-point theoretical scaling constant.

### `linear_performance.png`

Plots the empirical $B/A$ curve and the scaled theoretical proxy. The x- and
y-axes automatically switch to logarithmic scaling when their configured
thresholds are exceeded.

### `poisson_validation.png`

Shows the benign inter-arrival-time and fixed-window count diagnostics.

## Reproducibility

Use the same command-line parameters and `--seed` value to reproduce a run.
Changing the seed changes the sampled Poisson inter-arrival times and the small
within-burst jitter. Constant malicious timestamps are deterministic once the
benign-trace duration and malicious-flow count are fixed.

## Methodological notes

- The complete synthetic trace is generated and sorted before LINEAR begins.
- The benign-rate estimator uses the entire generated benign trace
- LINEAR processes the merged trace one flow at a time and does not use future
  labels when assigning the current price.
- Flow labels are used only after pricing to update the experimental costs.
- The experiment keeps the total flow count and benign fraction fixed.
- The homogeneous Poisson model assumes independent benign arrivals with a
  constant average rate.
- All timestamps and labels are stored in memory; very large experiments
  therefore require more memory.
