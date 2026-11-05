> **Dated design notes.** The dry-run status below describes the September 11 revision. Alpaca paper execution was subsequently enabled; see [PAPER_LAUNCH_2026-09.md](PAPER_LAUNCH_2026-09.md). New deployments still default to dry run.

# Comparison and execution revision — September 2026

The source backup before this revision is `review-backup-20260910.zip`.
Existing DynamoDB TARGET/SNAP records are retained. The default new experiment
is `cash-v2-20260911`; no historical performance is rewritten or backfilled
using today's model. Initial entry costs can make the first point slightly negative.

## Seven shadow books, equal initial budgets

Each book starts with $12,000 cash. Portfolio equity is cash plus invested
value; cumulative return is equity / initial budget - 1. There are no external
cash flows and no interest on cash. TSMOM, RSI, ML and ensemble allocate 1/N
of current equity per eligible universe name; inactive slots stay in cash.
Rotation allocates 1/3 per selected name, and dual momentum leaves a slot in
cash if its selected name fails the absolute filter. Both rerank daily.

Buy & hold initially equal-weights the same universe, pays the same modeled
entry cost, and retains its synthetic units. It is a comparison reference,
not an extra broker strategy. These are *fractional synthetic units*, not
whole shares or verified broker positions. Dividend/split-adjusted price
ratios represent reinvested total-return approximations; historical adjusted
prices are reread and units rescaled to preserve value across revisions.

All books rebalance at the session close using signals available from the
previous session. Old holdings earn the price move before that rebalance.
Costs are 5 bps on each buy and sell. Sells precede buys, purchases are scaled
to remaining cash including costs, and no negative cash/leverage is allowed.
The snapshot records cash, invested value, equity, costs and returns.

One immutable RUN record holds the entire daily signal batch; any missing
history or strategy failure prevents publication. One immutable LEDGER record
holds all seven books. Reporter reruns republish stored results. Missing
valuation data fails visibly rather than silently substituting old prices.
If a session was missed, replay its stored RUN via the reporter's as_of_date
in chronological order before continuing. A RUN that never existed cannot
be recreated by the reporter. Never backfill using future signals.

Experiment settings freeze the universe, budget, cost, lineup and model hash.
To change them: choose a new ExperimentId in `aws/samconfig.toml`, set the
other values there, and redeploy. Old experiment data remains queryable.
`make watchlist` updates SSM and the saved Watchlist override; changing its
universe also requires a new ExperimentId and redeploy.

## Broker execution is a separate approximation

DryRun remains true. When explicitly enabled later, six active strategies
use fixed $12,000 budgets and whole shares at recent unadjusted trade prices;
their aggregate maximum budget is $72,000. Shadow books compound equity,
so these are not expected to match the broker portfolio exactly.

The executor requires a fresh complete RUN, matching watchlist, active paper
account, no open orders and usable recent prices. An atomic DynamoDB lease
blocks overlapping executor invocations; it outlives the Lambda timeout and
only its owner can release it. Pending/partial orders block new submissions
until reconciled. A saved plan and stable client IDs
make retries reuse prior orders, including recovery after an uncertain HTTP
response. Rejected/canceled orders are not repeatedly resubmitted that day.
Only actual broker positions update BOTPOS. Filled quantities and average
fill prices are recorded separately from submitted quantities.

Sells precede buys; only existing cash is reserved for buys, using the smaller
of cash and non-marginable buying power when available. Pending sale proceeds
are not assumed spendable. A 2% estimate buffer applies to sizing and order
caps; it is not a guaranteed market fill price. The account still needs to
be dedicated to this bot. This is not a full settlement-aware cash broker
ledger, and automated execution still needs a supervised paper shakedown.

Schedules use America/New_York: signal 15:40, executor 15:45, reporter 17:15.
Holidays are skipped using Alpaca's calendar. The executor skips early-close
sessions when its usual schedule is after close; this differs from the shadow
close rebalance. No live-money endpoint is permitted by the executor.

## Research and reproducibility

`ml_train.py` now uses equal evaluation dates, gives TSMOM its pre-window
history, rejects gaps in OOS predictions and excludes the unscored final
horizon. The existing label-resolution purge is retained. Candidates export
to `models/candidates/logreg.json` by default; the deployed model is unchanged.
Training arguments, feature hash, label-resolution date, OOS predictions and
the Sharpe table are saved. Existing results in README are historical and
must be rerun with the corrected evaluator before using them as evidence.

The single-asset vectorized backtester retains its idealized shifted
close-to-close convention, which is faster but differs from the AWS schedule.
Its trade statistics now include exit costs and count completed round trips
only. For rotation and schedule-matched accounting, use the new portfolio
backtester; it shares the cash engine with the reporter.

```bash
python -m unittest discover -s tests -v
node --test dashboard/src/metrics.test.js
python portfolio_backtest.py --csv data/orcl-1995-2014.txt data/nvda-1999-2014.txt --strategy xsmom
python ml_train.py --symbols SPY MU --start 2005-01-01 --out models/candidates/logreg.json
make build
cd aws
sam deploy
```

On Windows, `py -3.11` can replace `python`. No testing command calls AWS or
submits real orders. The portfolio CLI produces a strategy and buy-and-hold
CSV over identical aligned dates with shared costs and starting capital.

The dashboard ranks cash-inclusive returns, labels gaps in percentage points,
shows cash and equity, and refreshes every minute. Sample data requires
`?sample=1`; failed production fetches never silently become a sample.
The legacy display is labelled until schema-v2 data is published. Old
dashboard assets are retained during uploads to avoid breaking cached pages.

API references used: [Alpaca orders](https://docs.alpaca.markets/us/docs/working-with-orders),
[open-order listing](https://docs.alpaca.markets/us/reference/getallorders-1),
[SAM ScheduleV2](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/sam-property-function-schedulev2.html).

## Deployment verification, 2026-09-11

- 24 Python tests and 3 dashboard metric tests passed. SAM and dashboard
  production builds succeeded. Vite reports a non-blocking 500 kB bundle warning.
- The portfolio CLI completed a dual-momentum comparison on the three bundled
  price files with evaluation starting 2012-01-01.
- AWS signal invocation published 84 target weights: seven books over 12
  tickers. The reporter published schema-v2 data for 2026-09-11.
- Each live book has a $12,000 initial budget, nonnegative cash, equity equal
  to cash plus investments, and return equal to equity / initial budget - 1.
- The executor's deployed configuration has DryRun=true. Its after-close
  smoke check acquired/released the lock and skipped orders because the market
  was closed. Broker submission/partial-fill scenarios were tested with fakes;
  an actual market-session order run remains untested.
- The live dashboard's seven books, percentage ranking, percentage-point gap,
  and percent/dollar controls were checked in the browser without console errors.
  There is initially one recorded session; a line chart needs the next session.
- The deployed model is unchanged. Its file SHA-256 is
  `2f3fad35a37c4035aa1dbe5fb35fe0717c997100683d1fc362a49c6d25545db3`.

Dashboard: https://d3vpu1cg2par0x.cloudfront.net/

## Execution revision — October 8, 2026

Shadow-ledger accounting is unchanged; these affect broker execution only.

- **Lots carry forward.** Each book's shares in a name keep their count while
  the book still holds it at the same weight and the position's value stays
  within 20% of its dollar target. On entry, or outside that band, the lot is
  re-sized at the current price. Previously every lot was re-sized daily, so
  whole-share rounding produced 1–4 share trades on days when no signal changed
  (SLB and FCX, September 25 – October 5). Plans now record per-book lots; the
  first plan after the change sizes every lot fresh.
- **One symbol no longer blocks the day.** A symbol that is not tradable, or has
  no trade in the last 15 minutes, waits for the next run while the others
  trade. The run is skipped only when no symbol can trade.
- **A late bar is tolerated once.** If a symbol's bar for the previous session
  has not arrived, its signals use the session before, and the RUN records it
  under `late_symbols`. Two or more missing sessions still block the batch.
- **Reads retry.** Alpaca GET requests make up to three attempts on HTTP 429/5xx
  and network failures. Order submission is never retried; lost responses are
  still recovered by client order ID.
