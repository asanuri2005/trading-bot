# Alpaca paper-trading readiness — September 24, 2026

The September 11–23 shadow run passes the operational audit. Following user
approval, **Alpaca paper execution was activated September 24 at approximately
15:55 New York time**. DryRun=false is deployed and saved in samconfig.toml.
The first supervised run submitted 12 buys; all 12 filled in the paper account.
Approximately $47,575.78 was invested, leaving $52,424.22 cash at the first
post-fill check. No real-money trading endpoint is enabled.

## Evidence

- Nine completed trading sessions have all nine RUN batches and nine atomic
  seven-book LEDGER records. No missing sessions or duplicate dates.
- Eight market-session dry-run previews exist (September 14–23). September 11
  was initialized after close, so no preview was expected that day.
- Lambda metrics since September 11: signal 10 invocations, executor 10,
  reporter 11; zero errors and zero throttles in all three. The deployed
  source matched the repository before the readiness fixes.
- Every book/day passes cash + invested value = equity, equity / $12,000 - 1
  = cumulative return, and daily P&L/return consistency checks. Cash is
  nonnegative. Buy & hold incurs no rebalancing costs after entry.
- Experiment settings and model remain consistent across all nine sessions.
  The SSM watchlist matches the frozen experiment; no active executor lock.
- Dashboard data is dated September 23, the latest completed session at the
  time of this audit, and matches the ledger. The browser renders all seven
  books and their percentage chart.
- Alpaca paper account: ACTIVE, USD, $100,000 equity/cash, no positions,
  zero orders since September 11 and no open orders. Trading/account blocks
  and user suspension are false. All 12 tickers are active and tradable.
- All three schedules are enabled in America/New_York: signal 15:40,
  executor 15:45, reporter 17:15. No fixed UTC offset/DST conversion is needed.

Read-only audit details (not committed):
`output/review/readiness-20260924.json`. These observations were collected
at approximately 15:32 New York time, before today's scheduled batch.

## Observed returns through September 23

| Shadow book | Return after modeled costs |
|---|---:|
| Dual momentum | +3.41% |
| Rotation top-3 | +3.41% |
| RSI-2 + 200d | +0.70% |
| TSMOM 6mo | -0.67% |
| ML LogReg | -0.83% |
| Ensemble 2-of-3 | -1.08% |
| Buy & hold | -1.34% |

These are nine-session observations, too short to establish persistent
outperformance. The equal momentum results reflect matching allocations.
The ML book has seven holdings on September 23; it is not stuck in the
initial buy-and-hold allocation.

## Readiness fixes

The reporter previously used order statuses saved by the executor at 15:45.
Fills arriving later would not be refreshed until the next executor run.
The reporter now refreshes pending orders and uncertain submissions from
Alpaca before publishing. It acquires the same DynamoDB lease as the executor
to avoid competing writes, and never submits or cancels an order. If refresh
fails or the lease is busy, the dashboard labels the saved order data stale.

The executor also checks user suspension and current asset tradability,
refuses to adopt unrelated account holdings, validates the saved plan's run
ID, and logs each completed execution/skip result. State scans use strongly
consistent reads. These changes do not alter strategy decisions or books.

All 32 Python tests pass, including eight new checks covering delayed fills,
refresh failure, lease contention, suspension, unavailable assets, unrelated
holdings, live-endpoint rejection, stale prices, sale-proceeds handling and
mismatched plans. The three dashboard metric tests pass. SAM validation and
production builds succeed; the existing Vite bundle-size warning remains.

## Activation configuration

- Paper endpoint only: `https://paper-api.alpaca.markets`.
- Six active strategies, each with a fixed $12,000 broker sizing budget.
  Maximum aggregate target allocation is $72,000 before share rounding.
- Whole-share, long-only market orders; same-ticker targets are combined
  into one portfolio in Alpaca. There are not six separate broker accounts.
- Buy & hold stays a shadow benchmark and sends no broker orders.
- Sizing uses cash, not the account's $400,000 margin buying power. Buys
  reserve estimated cost plus 2%; sale proceeds are not spent in the same
  invocation. The $15,000 per-order check also uses that buffer, which is an
  estimate rather than a guaranteed market fill price.
- Open orders block new submissions; stable client IDs protect retries.
- The existing September 11 comparison history and frozen ML model continue.
  Broker P&L starts when paper orders begin and will differ from the shadow
  books (different start, whole shares, 15:45 fills, fixed budgets and costs).

Today's fresh RUN (`8073725b09c787ef889c0f65`) produced 84 target weights.
The updated AWS executor's dry-run invocation proposed 12 buys with no
blocked actions. At 15:40, current quotes valued those proposed orders at
approximately $48,508 including the 2% buffer, within $100,000 cash and the
$15,000 per-order cap. Actual activation will size against then-current prices.
The reporter's cloud smoke check refreshed broker status successfully and
preserved all seven books and nine history points through September 23.

## Launch and pause

Activation is a CloudFormation parameter-only change: DryRun=true → false,
with every other parameter retained. Inspect the prepared change set, then
execute it when launching. Verify the executor's deployed DRY_RUN value and
update `aws/samconfig.toml` to match so a future deploy does not revert it.

Executed activation change set: `enable-paper-20260924`. Detailed pre-launch order
preview and change-set ARN: `output/review/paper-activation-20260924.json`.
The saved deployment configuration now has DryRun=false. All nine original
completed sessions remain under `cash-v2-20260911`; nothing was reset.

The first run was invoked manually after the day's scheduled 15:45 dry run.
Future execution remains scheduled for 15:45 New York time on trading days.
Broker order/position evidence: `output/review/paper-launch-20260924.json`.

The launch exposed timestamp variation between Alpaca's initial acceptance
response and subsequent order reads. Timestamp-keyed observations made 12
broker orders appear as 22 dashboard rows. Order records now use the stable
broker order ID with a fixed sort key; reads consolidate older observations
without deleting history. Two regression tests reproduce the issue; all
34 Python tests pass. Alpaca itself confirmed only the 12 intended orders.
After deployment, the public dashboard JSON contains exactly 12 filled
orders; every quantity and average fill price matches Alpaca's response.

The scheduled executor runs at 15:45 New York time. A supervised invocation
may be used during the open session after a fresh RUN exists. Inspect its
actions and Alpaca's actual accepted/filled quantities, then rerun the
reporter against the latest completed date to refresh the order table.
Rejected orders are deliberately not resubmitted with new IDs that day.

To pause new submissions, deploy DryRun=true and verify the Lambda setting.
This does not cancel already-open orders or sell existing holdings. Inspect
those separately before deciding what to do. Keep the paper account dedicated
to this bot; unrelated positions cause a visible skip.

Paper fills remain simulated. Alpaca documents their assumptions, including
partial fills and differences in dividends/costs, in its
[paper-trading specification](https://docs.alpaca.markets/us/docs/paper-trading).
Order IDs and status handling follow
[Alpaca's order API](https://docs.alpaca.markets/us/docs/working-with-orders).
