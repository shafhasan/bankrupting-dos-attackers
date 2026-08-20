# Resource-Competitive DDoS Client–Server Experiment

This folder contains the online client–server implementation used to evaluate **LINEAR** and **LINEAR-POWER** on CIC-DDoS2019 flow traces.

The experiment replays CIC-DDoS2019 CSV flow records through a local pricing server. All valid labeled flows are included, all source IPs are treated as logical clients, and all flows share one global pricing state. Protocol 17 records are replayed over UDP, protocol 6 records over TCP, and other protocol values are preserved as metadata while their flow messages are carried over the TCP replay socket.

The main wrapper scripts automatically start the pricing server and Weibull estimator, replay the dataset through LINEAR and LINEAR-POWER, evaluate the costs, generate the plots, and shut down the local services. You do **not** need to start the server or estimator manually.

## Requirements

- Python 3.9 or newer
- `numpy >= 1.26`
- `pandas >= 2.1`
- `scipy >= 1.11`
- `matplotlib >= 3.8`

Install the dependencies from `requirements.txt`.

## Setup

Clone the repository and enter the project directory:

```bash
git clone <repository-url>
cd <repository-directory>
```

Create and activate a virtual environment on macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Place the CIC-DDoS2019 CSV file either in the repository directory or provide its full path with `--csv`.

## Recommended Run: Fixed Calibration

The paper's current Experiment 3 configuration uses the **fixed-calibration** version. The earliest 200 benign flows are used to fit the initial Weibull distribution, and the resulting iteration length remains fixed for the entire run. The 200 calibration flows are still retained in the evaluation workload.

A complete run can be started with one command:

```bash
python run_fixed_calibration.py --csv "DrDoS_SSDP.csv" --output-dir DrDoS_SSDP --calibration-good-flows 200 --min-estimator-samples 30 --max-evaluation-flows 0 --speedup 0 --plot-every 500 --retry-delay 0.01 --good-flow-timeout 60 --socket-timeout 10 --max-attempts 64
```

For another trace, change only the CSV path and output directory. For example:

```bash
python run_fixed_calibration.py --csv "TFTP.csv" --output-dir TFTP --calibration-good-flows 200 --min-estimator-samples 30 --max-evaluation-flows 0 --speedup 0 --plot-every 500 --retry-delay 0.01 --good-flow-timeout 60 --socket-timeout 10 --max-attempts 64
```

The wrapper first runs **LINEAR** and then runs **LINEAR-POWER** on the same workload.

## Main Command-Line Options

| Option | Meaning | Default / paper setting |
|---|---|---:|
| `--csv` | Path to the CIC-DDoS2019 CSV file | required |
| `--output-dir` | Directory in which results are written | required |
| `--calibration-good-flows` | Number of earliest benign flows used for initial Weibull calibration | `200` |
| `--window-size` | Sliding-window capacity for benign inter-arrival samples | `200` |
| `--min-estimator-samples` | Minimum number of samples required for fitting | `30` |
| `--refit-every` | Number of new valid benign intervals between sliding-window refits | `10` |
| `--max-evaluation-flows` | Maximum number of flows to replay; `0` means all valid flows | `0` |
| `--speedup` | Wall-clock replay pacing; `0` removes artificial waiting while preserving logical timestamps | `0` |
| `--plot-every` | Plot one checkpoint every N serviced flows | `500` in the paper runs |
| `--retry-delay` | Logical trace seconds before a rejected LINEAR-POWER good flow retries | `0.01` |
| `--good-flow-timeout` | Maximum logical trace time allowed for a good flow; `0` disables the timeout | `60` |
| `--socket-timeout` | Wall-clock timeout for one local socket operation | `10` |
| `--max-attempts` | Maximum attempts for a LINEAR-POWER good flow | `64` |
| `--theorem2-M` | Optional manual value of Theorem 2 parameter `M`; normally computed automatically | automatic |

## What Happens During a Run

### Initial calibration

The controller orders all valid labeled flows by timestamp and selects the earliest 200 benign flows for calibration. Their inter-arrival times are fitted to a two-parameter Weibull distribution with location fixed at zero. The estimated mean benign inter-arrival time becomes the pricing iteration length.

### LINEAR

With `s` jobs already serviced in the current iteration, LINEAR uses

```text
PRICE = s + 1
```

LINEAR is evaluated as the zero-latency baseline. Each flow is serviced once at the exact current price, so LINEAR has no stale-price rejection or retry process.

### LINEAR-POWER

LINEAR-POWER uses

```text
PRICE = 2^floor(log2(s + 1))
```

A good flow begins with fee 1. If the fee is below the server's current price, the request is rejected, the current price is returned, and the flow retries after `--retry-delay`. Other original flows may be processed before the retry, so the returned price can become stale again. New original arrivals are processed before retries when both occur at the same logical timestamp.

Bad LINEAR-POWER flows use an informed minimum-fee strategy: the controller obtains the current server price and submits that exact amount.

## Cost Calculation

For every serviced flow, the server incurs one unit of service cost. The offline evaluator computes

```text
A = total service cost + accepted fees of good flows
B = total submitted fees of bad flows
B/A = adversary-to-algorithm cost ratio
```

For LINEAR-POWER, rejected good-flow fees are recorded but are not charged in `A`; only the final accepted good-flow fee contributes to the good-client component of Algorithm cost. This is an experiment-specific accounting rule.

Dataset labels are used for estimator calibration and offline evaluation, but the pricing server itself is label-blind when it assigns prices.

## Output Files

If the requested output directory is

```text
TFTP
```

then LINEAR and LINEAR-POWER results are respectively stored in

```text
TFTP/linear/
TFTP/linear_power/
```

After a successful run, each algorithm keeps the following outputs:

```text
evaluation_summary.json
good_flow_completion_metrics.csv
priced_flows_with_ground_truth.csv
theorem_proxy_metadata.json
B_over_A_linear_scale.png
B_over_A_log_scale.png
```

`evaluation_summary.json` contains the final Algorithm cost `A`, Adversary cost `B`, `B/A`, flow counts, protocol counts, and good-flow completion/rejection statistics.

`good_flow_completion_metrics.csv` contains one row for each evaluated benign flow, including its attempt count, rejection count, accepted fee, completion latency, and timeout status.

`priced_flows_with_ground_truth.csv` contains the serviced flows joined with the offline labels and cumulative Algorithm/Adversary cost accounting.

The two PNG files compare the empirical `B/A` curve against the corresponding scaled theoretical trend on linear and logarithmic y-axes.

Temporary server, controller, estimator, and evaluation files are removed after a successful run. If a run fails, those intermediate files are intentionally retained for debugging.

## Theoretical Plot Parameters

LINEAR is plotted against the scaled Theorem 1 trend. LINEAR-POWER is plotted against a scaled Theorem 2 proxy.

For LINEAR-POWER, `M` is computed automatically as the maximum number of original benign-flow arrivals occurring within any interval of length `--retry-delay`. Retry attempts are not counted as newly generated good jobs. The configured retry delay is used as an experimental proxy for the theoretical communication-delay parameter `Delta`.

To manually override `M`, add, for example:

```bash
--theorem2-M 5
```

## Repository Files

```text
common.py                  Shared socket/CSV utilities
estimator_service.py       Weibull estimator service
evaluate.py                Offline A, B, and B/A calculation
plot_b_over_a.py           Empirical/theoretical B/A plots
run_experiment.py          Runs LINEAR and LINEAR-POWER sequentially
run_fixed_calibration.py   Fixed-estimator wrapper
run_sliding_window.py      Sliding-window wrapper
server.py                  Global pricing server
trace_controller.py        Calibration and trace replay controller
requirements.txt           Python dependencies
```
