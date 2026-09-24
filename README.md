# Serverless Trading Bot — Research and Shadow Portfolio Comparison

**Current implementation:** seven cash-inclusive shadow books with equal
$12,000 starting budgets, including buy-and-hold. Old percentage rankings
excluded idle cash and should not be used to compare whole portfolios.
See [REVIEW_CHANGES.md](REVIEW_CHANGES.md) for current accounting, commands,
deployment settings and limitations. Research tables below predate the
evaluation fixes and are retained as historical notes, not fresh validation.

See [PAPER_READINESS.md](PAPER_READINESS.md) for the September 24 operational
audit, paper-account launch settings, and activation/pause procedure.

Vectorized daily backtester with pluggable strategies. This is the local
research layer of a larger AWS project (EventBridge → Lambda → DynamoDB →
Alpaca paper trading → React dashboard → Bedrock sentiment).

**This is a learning/portfolio project. Paper trading only. Nothing here is
investment advice, and backtest results do not predict future returns.**

## Quickstart

```bash
pip install -r requirements.txt

# live data (requires internet)
python backtest.py --symbol SPY --start 2000-01-01

# offline, using the bundled real historical data
python backtest.py --csv data/orcl-1995-2014.txt --strategy sma --fast 20 --slow 50
python backtest.py --csv data/nvda-1999-2014.txt --strategy tsmom --lookback 126

# systematic dip-buying (buy X% below the 20-day high; exit at target/stop/time)
python backtest.py --symbol MU --start 2005-01-01 --strategy dip \
    --drop 7 --target 10 --stop 5 --max-hold 3

# full lineup (7 strategies) across any set of assets
python compare.py --csv data/*.txt
python compare.py --symbols SPY MU AMD --start 2005-01-01
```

## Trading universe (halal screening)

The bot only trades tickers listed in `watchlist.txt`, populated manually
from the halal.sh triple-screened US list. Refresh it periodically —
compliance status changes as company financials change. The system is
long-only, cash-only, no margin, no shorting by design.

Outputs a metrics table (always benchmarked against buy & hold), an equity
curve + drawdown PNG, and a per-day results CSV in `output/`.

## Design decisions

- **No lookahead bias.** Signals are shifted one bar before becoming
  positions (`signals.shift(1)`), so a signal computed at day t's close
  earns day t+1's return at the earliest.
- **Costs modeled.** Every position change pays `--cost-bps` (default 5)
  to approximate slippage. Commission-free ≠ cost-free.
- **Strategies are plug-ins.** Implement `.signals(prices) -> Series` of
  {0,1}, register in `strategies.REGISTRY`, done. The same classes will be
  imported unchanged by the AWS Lambda signal generator in Phase 2.
- **Verified accounting.** An always-long strategy at zero cost reproduces
  buy & hold exactly.

## Validation results (real daily data, 5 bps/side)

| Run | CAGR | B&H CAGR | Sharpe | B&H Sharpe | MaxDD | B&H MaxDD |
|---|---|---|---|---|---|---|
| ORCL 1995–2014, SMA 20/50 | 6.5% | 16.8% | 0.35 | 0.57 | −74% | −84% |
| ORCL 1995–2014, TSMOM 126d | 6.9% | 16.8% | 0.37 | 0.57 | −64% | −84% |
| NVDA 1999–2014, SMA 20/50 | 22.1% | 17.4% | 0.66 | 0.57 | −61% | −90% |
| NVDA 1999–2014, Dip 7%/+10%/−5%/3d | **−13.9%** | 17.4% | −0.11 | 0.57 | −98% | −90% |
| ORCL 1995–2014, Dip 10%/+10%/−5%/3d | 18.9% | 16.8% | — | 0.57 | −54% | −84% |

Dip-buying findings: with a 3-day max hold, only ~11% of trades reached the
+10% target (59% timed out, 30% stopped out). The strategy re-buys all the
way down a sustained decline — on NVDA's 1999–2002 and 2008 crashes it
chained hundreds of small losses into −98%. And results swing wildly with
the entry threshold (ORCL: 4% → 19% CAGR moving the dip from 5% → 10%),
which is exactly what overfitting risk looks like. An obvious next
iteration: a regime filter (only buy dips above the 200-day SMA).

The takeaway: the identical strategy underperforms on one asset and
outperforms on another. One good backtest proves nothing — which is why the
pipeline (not the strategy) is the product here.

## Strategy comparison (7 strategies × 3 assets, Sharpe)

|  | ORCL | NVDA | YHOO | Mean |
|---|---|---|---|---|
| SMA 20/50 | 0.35 | 0.66 | 0.49 | 0.50 |
| SMA 50/200 | 0.32 | 0.52 | 0.75 | 0.53 |
| TSMOM 6mo | 0.37 | 0.60 | 0.79 | **0.59** |
| Dip 7/+10/−5/3d | 0.51 | −0.11 | 0.24 | 0.21 |
| Dip + 200d filter | 0.28 | 0.07 | 0.17 | 0.17 |
| RSI2<10 + 200d | 0.70 | −0.00 | 0.53 | 0.41 |
| Donchian 55/20 | 0.27 | 0.62 | 0.66 | 0.52 |
| MACD 12/26/9 | 0.14 | 0.41 | 0.31 | 0.29 |
| Bollinger 20/2σ + 200d | 0.56 | 0.20 | 0.66 | 0.47 |
| 52wk-high ±5% | 0.20 | 0.16 | 0.67 | 0.34 |
| TSMOM vol-target 15% | 0.27 | 0.50 | 0.68 | 0.48 |
| **Buy & hold** | 0.57 | 0.57 | 0.57 | **0.57** |

Second-wave notes: MACD's fast trend signal whipsaws (worst trend variant
here). Bollinger reversion behaves like RSI2 — decent, same family.
Vol-targeted TSMOM *underperformed* plain TSMOM on these names: with
realized vol of 40–80% against a 15% target, exposure stays clipped low
while daily fractional rebalancing pays costs — vol targeting earns its
reputation on index-vol assets (test it on SPY), and a smarter version
would rebalance only outside a drift band. Research-only until the signal
Lambda supports fractional sizing.

Findings: (1) no strategy consistently beat buy-and-hold on returns; only
TSMOM edged it on mean Sharpe, within noise. (2) Trend following (TSMOM,
Donchian, golden cross) was the most consistent family — low win rates,
big winners, positive everywhere. (3) Mean reversion (RSI2) is the
opposite personality: ~71% win rate, ~10% time in market, and by far the
smallest drawdowns (−18% on ORCL vs −84% holding) — but it flatlined on
NVDA. (4) The 200-day regime filter fixed the dip strategy's catastrophe
(NVDA −13.9% → −1.8% CAGR) but killed its ORCL returns: it's damage
control, not alpha. (5) Every strategy has an asset where it looks bad —
which is why single-backtest conclusions are worthless. Next validation
steps: run on SPY + current watchlist names locally; add a walk-forward
(train/test split) mode.

## Phase 2: AWS pipeline (`aws/`)

EventBridge (weekdays 19:40 / 19:45 UTC) → **SignalFunction** (Alpaca daily
bars → strategy lineup → DynamoDB targets) → **ExecutorFunction**
(aggregate targets vs. actual positions → market orders on Alpaca paper).
Secrets live in SSM Parameter Store. Live lineup: TSMOM 6mo + RSI2 side by
side (the A/B from the comparison), each sized to `AllocationUsd` per
ticker. The exact same `strategies.py` powers backtests and Lambda.

### Deploy

Prereqs: AWS account, SAM CLI, free Alpaca paper account.

```bash
# 1. Store Alpaca keys as SecureStrings (CloudFormation can't create these).
#    Keys live ONLY here — never in code, git, or chat.
aws ssm put-parameter --name /trading-bot/alpaca/key_id --type SecureString --value "<KEY_ID>"
aws ssm put-parameter --name /trading-bot/alpaca/secret  --type SecureString --value "<SECRET>"

# 2. Test offline, then deploy (pick your region's PandasLayerArn when prompted)
make test
make deploy

# 3. Smoke test without waiting for the schedule
aws lambda invoke --function-name <SignalFunctionName> /dev/stdout
aws lambda invoke --function-name <ExecutorFunctionName> /dev/stdout

# 4. After a few days of sane dry-run logs, arm it:
sam deploy --parameter-overrides DryRun=false

# Edit the halal watchlist anytime, no redeploy:
aws ssm put-parameter --name /trading-bot/watchlist --type String --overwrite --value "MU,XXXX"
```

### Safety rails

`DRY_RUN=true` by default (logs intended orders, places none); hard skip
when the market is closed; stale signals (>6h) ignored; any order above
`MAX_ORDER_USD` refused; only symbols in targets or previously bought by
the bot are ever touched — run it in a dedicated paper account. Whole
shares only; signals computed from completed daily bars.

Cost: two short invocations per weekday + on-demand DynamoDB ≈ free tier.

## Phase 3: Dashboard (`dashboard/` + reporter Lambda)

A third Lambda (**ReporterFunction**, weekdays 21:15 UTC — after the close)
marks each strategy book to market, appends a per-strategy daily snapshot
to DynamoDB, and publishes `data.json` to a private S3 bucket served
through CloudFront (Origin Access Control; the bucket itself is never
public). The React app ("Paper Ledger") shows the two books racing:
per-book cumulative P&L cards, a head-to-head chart with a live scoreline,
today's targets, and recent fills.

Accounting matches the backtest engine: yesterday's snapshot positions earn
today's close-to-close move; same-day reruns can't double-count. Fills are
approximate (signal close) — exact fills are auditable in Alpaca by order id.

```bash
# preview locally with sample data (no AWS needed)
cd dashboard && npm install && npm run dev

# after `make deploy` has created/updated the stack:
make dashboard            # builds + syncs to S3 + invalidates CloudFront
aws lambda invoke --function-name <ReporterFunctionName> /dev/stdout   # publish first data.json
```

The CloudFront URL is in the stack outputs. It's unauthenticated (paper
data only) — add CloudFront + Cognito or a signed-cookie setup if you ever
want it private.

## ML signal — built, walk-forward validated, honestly rejected (`ml_train.py`)

A pure-numpy logistic regression predicts P(next 5 days up) from ten
technical features (multi-horizon returns, realized vol, RSI-2/14,
distance from the 50/200-day SMAs). Evaluation is strictly walk-forward:
quarterly refits that train only on labels fully resolved before each
cutoff, standardization from the training window only, predictions
stitched into a 14-year out-of-sample record and traded through the same
engine (with its one-day execution shift).

Result — 2001–2015 OOS, 60 refits, 10,800 predictions: direction accuracy
**0.506 vs 0.517** for always predicting "up"; mean OOS Sharpe **0.18 vs
TSMOM's 0.24** and buy-and-hold's 0.38, with 3–5× the trades. Per the
project's own rule (nothing goes live without earning it), **MLStrategy is
not in the live lineup.** The learned weights are at least economically
sensible — dist_sma200 +0.14 (uptrends persist), r126 −0.12 (stretched
rallies mean-revert short-term) — the signal is just too weak to pay for
its turnover.

**v2 experiments (pre-declared grid, same walk-forward referee):** adding
three features (vol-regime ratio, risk-adjusted momentum, an
oversold-in-uptrend interaction) lifted mean OOS Sharpe 0.18 → **0.23**;
extending the label horizon to 21 days *hurt* (0.14); raising the entry
threshold to 0.55 hurt badly (−0.04). Best config now ties TSMOM (0.23 vs
0.24) and still trails buy-and-hold (0.38) — **still benched.** The
exported model is the v2 winner (13 features, h=5). Lesson: logistic
regression is convex, so "training longer" changes nothing — gradient
descent already converged. The binding constraint is signal content, and
better inputs (not more epochs) is the only axis that moved the number.

The model still ships: `models/logreg.json` + `strategies.MLStrategy`
(pure-numpy inference, Lambda-ready, synced by `make sync`). Re-evaluate
on any assets:

```bash
python ml_train.py --symbols SPY MU --start 2005-01-01
```

To promote it if it ever earns it: add `MLStrategy()` to `get_lineup()` in
`aws/src/shared/runtime.py`, add a display name in `reporter_handler.py`,
then `make test && make deploy`. It is deliberately absent from
`compare.py`'s matrix — the exported model has seen that history, so only
walk-forward numbers count.

## Roadmap

1. ✅ Backtest engine + strategy plug-in system
2. ✅ SAM template: EventBridge schedule → signal Lambda → DynamoDB → executor
   Lambda → Alpaca paper API (keys in SSM Parameter Store)
3. ✅ React dashboard on S3 + CloudFront + reporter Lambda (P&L race, trade log)
4. Bedrock news-sentiment signal blended into the registry
5. ✅ Walk-forward validation + ML strategy (built, evaluated, rejected from live lineup on the evidence)
