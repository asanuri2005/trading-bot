# Changelog

Notable changes to the trading bot. Each release starts a new experiment, so
its results are compared only against books that ran under the same rules.

## v2.0.0 — 2026-11-23

Experiment `v2-20261123`, in a new Alpaca paper account starting at $100,000.
Design notes and backtest evidence: [docs/ACCOUNTING_AND_EXECUTION.md](docs/ACCOUNTING_AND_EXECUTION.md#version-2--november-23-2026-experiment-v2-20261123).

### Universe
- Expanded from 12 to 50 halal-screened stocks across nine sectors, chosen
  from the halal.sh triple-screened list. Only plainly "Compliant" stocks are
  included; those flagged Near Limit or Borderline are left out.
- Removed MU and WELL, which no longer pass the screen, and FCX and CEG,
  which are flagged Near Limit.

### Strategies
- **New: ML Trees (GBT).** Gradient-boosted trees trained with LightGBM and
  run in pure NumPy on Lambda, so the deployment gains no ML dependency.
- **Rebuilt: ML LogReg.** Now ranks stocks by the probability of beating
  their peers over 5 sessions and holds the top 5, instead of predicting
  whether each stock rises (which held nearly everything). It is the baseline
  the trees are measured against.
- **New: Hold all, breadth filter.** Holds every stock while most are above
  their 200-day average, otherwise cash.
- **Changed: Rotation** is now top 5 with a buffer: a holding is kept until it
  falls below 10th, which cut backtest turnover from 48x to 9x a year.
- **Removed: Dual momentum,** which matched Rotation on 99.9% of days.
- New walk-forward trainer, `ml_rank.py`, for the two ML books.

### Execution
- Fractional-share orders, so every stock fits a $12,000 book.
- Signals now use each stock's 15:40 price, acting the same session instead
  of a day later.
- Stock dividends paid in shares no longer stop the executor (this caused a
  missed session on November 20, 2026).
- Latest prices are fetched in one batch request, and the Alpaca client paces
  itself under the API rate limit.

### Dashboard
- Eight books, with a validated eighth series color.
- The order table shows only the current experiment's orders.

### Tests
- 77 Python tests (up from 51) and 6 dashboard tests. New coverage includes
  no-lookahead feature checks, numpy-versus-LightGBM equivalence, fractional
  sizing, the rotation buffer and the stock-dividend regression.

## v1 — 2026-09-11 to 2026-11-20

Experiment `cash-v2-20260911`: seven books over a 12-stock universe, with
whole-share paper execution from September 24. See
[docs/PAPER_LAUNCH_2026-09.md](docs/PAPER_LAUNCH_2026-09.md).
