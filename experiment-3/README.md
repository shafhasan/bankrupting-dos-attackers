# Trace-driven LINEAR and LINEAR-POWER experiments

This project replays a CIC-DDoS2019-compatible flow CSV on one machine. Dataset source IPs are logical clients. Dataset destination IPs and original IP Protocol values are retained as workload metadata, but all flows share one global pricing server state. The operating-system sockets use `127.0.0.1`, while original IPs, ports, protocol, timestamps, labels, and flow features are preserved during replay and in the final analysis outputs.

The same wrapper command runs both algorithms:

1. **LINEAR** writes directly into the requested `--output-dir`.
2. **LINEAR-POWER** writes into `<output-dir>/linear_power/`.


## Workload and client inclusion

Client sampling has been removed. For each CSV, every source IP represented by the valid evaluation workload is treated as a logical client. There is no `10`-client cap, no minimum-benign-flow requirement, and no IP-Protocol filter.

The controller first orders all labeled rows with valid timestamps and uses the earliest `--calibration-good-flows` BENIGN rows (default 200) to initialize the Weibull estimator. Those calibration rows are **not removed** from the measured workload: the evaluation replays every valid labeled flow in the CSV, including the 200 calibration flows. Original protocol 17 is carried over the UDP replay socket and protocol 6 over the TCP replay socket. Other protocol values are also included and retain their original `Protocol` value as metadata, but their JSON replay messages are carried over the TCP data socket because the experiment is flow-level rather than a packet-level protocol emulator. Rows without a usable label or timestamp still cannot be classified or scheduled and are excluded.

The legacy options `--benign-client-count`, `--min-benign-flows-per-client`, and `--seed` are accepted only for backward compatibility and are ignored.

## Global pricing state

The implementation now follows the paper's single-server pricing model directly. There is exactly one pricing state for the whole run:

- one global iteration ID and iteration start time;
- one global within-iteration serviced-job counter;
- one global LINEAR or LINEAR-POWER price;
- one global pending estimator update applied at the next iteration boundary.

`Destination IP` remains in every message and log row for trace fidelity and offline analysis, but it does not select a separate price counter or iteration. A job to one destination therefore changes the price seen by the next job even when that next job has a different destination IP. The server records `logical_server_key=GLOBAL` for every serviced job and attempt.

## Retry scheduling

LINEAR is the zero-latency baseline: each flow is serviced once at the exact current LINEAR price, with no rejection or retry. LINEAR-POWER uses the nonblocking rejection/retry mechanism. For LINEAR-POWER, the controller uses a deterministic discrete-event scheduler:

1. A new dataset flow arrives at its original CSV timestamp.
2. A rejected good flow is reinserted at:

```text
next_retry_time = current_attempt_time + retry_delay
```

3. Any new flows whose timestamps occur before that retry are sent first.
4. New flows win ties when a new arrival and retry have the same logical time.

This permits other flows, including flows with the same or different destination IPs, to be serviced between two attempts of a rejected good flow. The server lock is held only for the atomic price check and state update of one request; it is not held while a client waits to retry. A good flow begins with fee 1 under either algorithm, updates its fee to the returned required price after a rejection, and retries later. A bad flow uses the informed minimum-fee strategy and submits the current quoted price.

The scheduler is deterministic rather than thread-per-flow. That avoids creating hundreds of thousands of threads and makes repeated runs with the same arguments reproducible.

Configure retry spacing with:

```text
--retry-delay 0.01
```

The value is in original trace seconds and is independent of `--speedup`.

## Fee accounting

The server remains label-blind. It logs both:

- `accepted_fee`: fee attached to the successful attempt;
- `submitted_fee_sum`: sum of fees attached to every attempt.

The offline evaluator applies the requested label-specific charging rule:

```text
Good flow cost = accepted_fee only
Bad flow cost  = submitted_fee_sum
```

Rejected good-flow fees remain represented in the per-flow completion/accounting fields and in the summary field `total_good_rejected_fee_not_charged`, but they do not contribute to algorithm cost A.

This accepted-only good-flow rule is a custom experimental rule. It differs from the paper's total-fee accounting, where repeated submitted fees are included in client cost.

## Good-flow completion metrics and timeout

Each algorithm directory contains:

```text
good_flow_completion_metrics.csv
```

It records one row for every evaluated good flow, including:

- logical completion latency in trace seconds;
- wall-clock completion latency;
- attempt count;
- rejection count;
- final accepted fee;
- final required server price;
- deadline;
- completion status;
- whether it completed before timeout;
- whether it timed out.

The logical timeout is configured with:

```text
--good-flow-timeout 60
```

The value is in original trace seconds. Set it to `0` to disable logical flow timeouts. A retry is not sent if its scheduled time would exceed the deadline.

The separate option:

```text
--socket-timeout 10
```

is a real wall-clock timeout for one local TCP or UDP request. It protects the experiment from a non-responsive process and is not the good-flow completion deadline.

## Algorithms

### LINEAR

With `s` jobs already serviced globally in the current iteration:

```text
PRICE = s + 1
```

LINEAR follows the paper's zero-latency model in this experiment. The current LINEAR price is treated as known at submission time, so the server charges exactly that price and services the flow in one attempt. LINEAR does not use the retry delay, bounce logic, or maximum-attempt rule.

### LINEAR-POWER

```text
PRICE = 2^floor(log2(s + 1))
```

For LINEAR-POWER, a good job starts with fee 1. When rejected, it stores the returned price and retries at the scheduled retry time. Because other jobs can be serviced in between, the returned price may be stale by the next attempt, and one good job can be rejected multiple times. LINEAR does not use this asynchronous retry path.

The existing adversary model remains unchanged: a bad job obtains an exact current quote and submits that price once.

## Estimator and iteration behavior

- Initial benign calibration fits a zero-location Weibull distribution.
- The estimated mean benign inter-arrival time becomes the iteration length.
- Fixed mode freezes that estimate.
- Sliding mode refits after `--refit-every` new valid benign intervals.
- All destination IPs and all original protocol values share one global pricing state and one iteration clock.
- Mathematical iteration boundaries are preserved, including empty iterations.
- Retry attempts use their scheduled logical arrival time, so a retry may cross into a later iteration.
- Sliding estimator observations occur once per original dataset flow, not once per retry. The calibration benign rows are not observed a second time by the sliding estimator when they are replayed for pricing.

## Example fixed run

```bash
python run_fixed_calibration.py \
  --csv "DrDoS_DNS.csv" \
  --output-dir runs/fixed \
  --calibration-good-flows 200 \
  --window-size 200 \
  --min-estimator-samples 30 \
  --refit-every 10 \
  --max-evaluation-flows 0 \
  --speedup 0 \
  --plot-every 500 \
  --retry-delay 0.01 \
  --good-flow-timeout 60 \
  --socket-timeout 10
```

## Example sliding run

```bash
python run_sliding_window.py \
  --csv "DrDoS_DNS.csv" \
  --output-dir runs/sliding \
  --calibration-good-flows 200 \
  --window-size 200 \
  --min-estimator-samples 30 \
  --refit-every 10 \
  --max-evaluation-flows 0 \
  --speedup 0 \
  --plot-every 500 \
  --retry-delay 0.01 \
  --good-flow-timeout 60
```

## Important outputs

After a successful LINEAR or LINEAR-POWER run, only the paper-facing outputs are retained in that algorithm's run directory:

- `evaluation_summary.json`: aggregate A, B, B/A, evaluation/serviced protocol counts, serviced-flow, latency, completion, fee, and rejection statistics;
- `good_flow_completion_metrics.csv`: one row per evaluated good flow with completion latency, attempts, rejections, final accepted fee, and timeout status;
- `priced_flows_with_ground_truth.csv`: serviced flows joined with offline labels and cumulative Algorithm/Adversary cost accounting;
- `theorem_proxy_metadata.json`: theorem-proxy metadata, scaling constant, and for LINEAR-POWER the computed M and retry-delay Delta proxy;
- `B_over_A_linear_scale.png`: empirical B/A versus the scaled theorem proxy on a linear y-axis;
- `B_over_A_log_scale.png`: the same comparison on a logarithmic y-axis.

Intermediate files needed during replay, evaluation, and plotting are deleted only after the run finishes successfully. On a failed run, they are retained for debugging. Per-client/per-destination summary CSVs, attempt-level CSVs, calibration JSON/CSV files, estimator-update CSVs, plot-data CSVs, and `algorithm_comparison_summary.json` are no longer written as persistent outputs.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Automatic Theorem 2 M calculation

For LINEAR-POWER plots, `M` is now computed automatically unless `--theorem2-M` is supplied manually.
The implementation uses the configured logical `--retry-delay` as an experimental proxy for the
Theorem 2 delay interval `Delta`, then computes

```text
M = maximum number of original BENIGN flow arrivals in any interval of length retry_delay
```

Only original generated good flows from `evaluation_ground_truth.csv` are counted; retry attempts are
not counted. The selected value, its source, the delay proxy, and the final plot scaling constant are
written to `theorem_proxy_metadata.json` in each run directory.

A manual override remains available:

```bash
--theorem2-M 5
```

When the option is omitted, the normal experiment command automatically calculates `M` for the
LINEAR-POWER run. LINEAR continues to use the Theorem 1 plotting branch and does not use `M`.
