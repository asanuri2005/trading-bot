# GUIDE — the whole project in plain language

> The operating model has changed: use [REVIEW_CHANGES.md](REVIEW_CHANGES.md)
> for the current seven-book comparison, cash accounting, tests and deployment
> settings. The older two-book examples and performance conclusions below
> are historical. The deployed ML weights are frozen; candidates train separately.

## 1. What this actually is

Three layers, one idea: **never trust a trading rule you haven't tested,
and never run one by hand that a robot can run for you.**

1. **The research lab (runs on your laptop).** Replays trading rules
   against decades of real price history to see what they *would* have
   done. This is where rules earn or lose credibility.
2. **The robot (runs on AWS).** Every weekday: at 3:40pm ET a Lambda looks
   at recent prices and decides "be in or be out" for each strategy; at
   3:45pm a second Lambda places paper-money orders through Alpaca to
   match; at ~5:15pm a third marks the books and updates the dashboard.
   No human in the loop.
3. **The window (dashboard).** A React page on S3 + CloudFront showing the
   two live strategies racing: P&L curves, positions, today's calls,
   recent fills.

Current live setup: **TSMOM 6mo** (hold while the past 6 months are up —
a trend follower) vs **RSI-2 + 200d** (buy sharp 1–2 day panics inside
uptrends, sell the bounce — a dip buyer), $1,000 paper each, ticker list =
your halal watchlist (currently MU), **dry-run ON** (logs intended orders,
places none) until you flip it.

## 2. What the numbers mean

| Term | Plain meaning | In this project |
|---|---|---|
| Backtest | Replay a rule on history | The engine only ever trades on information it would've had that day (no lookahead) |
| Signal | The rule's daily verdict | 1 = hold the stock, 0 = sit in cash |
| Total return | End-to-end % change | "+1,179%" = $1k → $12.8k over the period |
| CAGR | Smoothed % per year | TSMOM on MU: 31.8%/yr vs 22.9% just holding |
| Sharpe ratio | Return per unit of bumpiness — the ride-smoothness score, and the fair way to compare strategies with different risk | ~0.3 rough · ~0.6 = typical buy-and-hold · 1.0+ genuinely good · 2+ elite. This is the number all the rankings use |
| Max drawdown | Worst peak-to-valley loss you'd have lived through | Holding AMD: −96% (a $10k account touched $400). A −50% loss needs +100% to get back |
| Time in market | % of days invested vs in cash | RSI-2 ≈ 10% — it's usually waiting |
| Round trips | Completed buy→sell cycles | |
| Win rate | % of round trips profitable | The paradox: TSMOM wins ~33% of trades and made the most money (rare huge wins); RSI-2 wins ~73% (many small wins). Win rate ≠ profit |
| 5 bps/side | Cost model: 0.05% per buy or sell | Keeps results from being fantasy; frequent traders bleed here |
| In-sample | Tested on data the rule was tuned on | Flattering, untrustworthy |
| Out-of-sample (OOS) | Tested on data it never saw | The only evidence that counts |
| Walk-forward | Train on the past → predict the next quarter → roll forward, repeat | How the ML was judged: 60 refits, 10,800 OOS predictions |
| Base rate | How often "always guess up" is right | 51.7% of 5-day windows. The ML scored 50.6% — *below* the dumbest baseline, which is why it's benched |
| Dry run | Bot logs what it would trade, trades nothing | Default-on safety switch |

## 3. What the research concluded

1. Your discretionary dip-buying style, made mechanical, loses — it
   re-buys all the way down crashes (571 chained trades, −91% on one test).
2. Across 11 strategies × 6 assets × two eras, **nothing reliably beat
   buy & hold on raw returns.** Trend following came closest and was the
   most consistent; on MU it genuinely won (31.8% vs 22.9% CAGR, out of
   sample).
3. What strategies reliably buy is **smaller drawdowns** — TSMOM cut MU's
   worst loss from −91% to −60%. Fewer catastrophic holes to climb out of.
4. The ML signal was built with full rigor and **rejected on the
   evidence** (OOS Sharpe 0.23 vs TSMOM 0.24 vs holding 0.38). More
   training cannot fix it — the model converges; the inputs just don't
   carry enough signal.
5. Every strategy looked bad somewhere. One good backtest means nothing;
   consistency across assets and eras is the bar.

## 4. What each file does

**Research lab (repo root)**
- `data_loader.py` — gets prices (yfinance live, or bundled CSVs offline)
- `strategies.py` — the rulebook: all 11 strategy classes + ML features.
  One file powers both backtests and the live Lambda
- `backtest.py` — the referee: metrics + equity/drawdown chart for one run
- `compare.py` — the tournament: every strategy × every asset → Sharpe matrix
- `ml_train.py` — the ML lab: walk-forward training, honest OOS scoring,
  exports `models/logreg.json`
- `data/`, `output/`, `models/` — inputs and results
- `watchlist.txt` — local notes; the *live* gate is the SSM parameter

**Robot (`aws/`)**
- `template.yaml` — the entire cloud stack as code (SAM/CloudFormation)
- `src/signal_handler.py` — the brain (3:40pm ET): prices → signals → targets
- `src/executor_handler.py` — the hands (3:45pm): targets vs positions →
  orders. Rails: dry-run, market-clock check, stale-signal cutoff,
  max-order cap, only touches watchlist tickers
- `src/reporter_handler.py` — the scorekeeper (5:15pm): daily P&L
  snapshots → publishes `data.json`
- `src/shared/` — `alpaca.py` (broker client), `runtime.py` (config +
  **`get_lineup()` = what actually trades**), `state.py` (DynamoDB shapes)
- `local_test.py` — flight simulator: 9 end-to-end scenarios, no AWS needed
- `scripts/deploy_dashboard.sh` — builds and ships the dashboard

**Window (`dashboard/`)** — Vite + React "Paper Ledger" app; sample data
included so it renders before AWS exists.

## 5. Complete runbook (Windows / Git Bash)

### Install once
1. **Python deps**: `pip install -r requirements.txt` (done already)
2. **AWS account** → IAM → create a user with AdministratorAccess →
   create access key
3. **AWS CLI**: install the Windows MSI from aws.amazon.com/cli, then
   `aws configure` (paste key, secret, region e.g. `us-east-1`)
4. **SAM CLI**: Windows MSI from
   docs.aws.amazon.com/serverless-application-model → "Install AWS SAM CLI"
   (or the quick route: `pip install aws-sam-cli`). Verify: `sam --version`.
   Docker is NOT needed for this project.
5. **Node.js LTS** from nodejs.org (dashboard build only)
6. **Alpaca**: free account at alpaca.markets → Paper Trading → generate
   API key + secret

> **No `make` on Git Bash?** Use the underlying commands:
> `make test` ≡ `cp strategies.py aws/src/ && cp models/*.json aws/src/models/ && python aws/local_test.py`
> `make deploy` ≡ `cd aws && sam build && sam deploy --guided`
> `make dashboard` ≡ `bash aws/scripts/deploy_dashboard.sh`

### Research anytime (no AWS)
```bash
python backtest.py --symbol MU --start 2005-01-01 --strategy tsmom
python compare.py --symbols SPY MU AMD --start 2005-01-01
python ml_train.py --symbols SPY MU --start 2005-01-01
python aws/local_test.py        # prove the pipeline offline
```

### Deploy the robot
```bash
# 1. secrets into SSM (only place they ever live)
aws ssm put-parameter --name /trading-bot/alpaca/key_id --type SecureString --value "<KEY_ID>"
aws ssm put-parameter --name /trading-bot/alpaca/secret  --type SecureString --value "<SECRET>"

# 2. deploy (guided first time: stack name "trading-bot", your region;
#    if not us-east-1, paste your region's AWSSDKPandas-Python312 layer ARN
#    from aws-sdk-pandas.readthedocs.io/en/stable/layers.html)
cd aws && sam build && sam deploy --guided && cd ..

# 3. smoke test immediately (names come from the deploy output)
aws lambda invoke --function-name <SignalFunctionName>   /dev/stdout
aws lambda invoke --function-name <ExecutorFunctionName> /dev/stdout
aws lambda invoke --function-name <ReporterFunctionName> /dev/stdout

# 4. dashboard
bash aws/scripts/deploy_dashboard.sh     # prints your CloudFront URL

# 5. confirm the live watchlist
aws ssm put-parameter --name /trading-bot/watchlist --type String --overwrite --value "MU"

# 6. after a few days of sane dry-run logs (CloudWatch → executor):
cd aws && sam deploy --parameter-overrides DryRun=false
```

### Change things later
- Live strategies → edit `get_lineup()` in `aws/src/shared/runtime.py`,
  then test + deploy
- Position size / caps → `sam deploy --parameter-overrides AllocationUsd=2000 MaxOrderUsd=8000`
- Tickers → the SSM watchlist command (instant, no redeploy)
- Costs: ~free tier — three short Lambda runs per weekday + a tiny table

Paper trading only. Nothing here is investment advice, and backtests do
not predict future returns.
