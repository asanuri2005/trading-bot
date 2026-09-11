"""Vectorized daily backtest engine + CLI.

Design decisions worth knowing (and explaining in interviews):

1. No lookahead: a signal computed from day t's close is only traded into
   on day t+1 (`signals.shift(1)`), so it earns day t+1's return at the
   earliest. Skipping this shift is the most common way backtests lie.
2. Costs: every position change is charged `cost_bps` basis points to
   approximate slippage/spread. Alpaca is commission-free, but fills
   aren't (5 bps default is deliberately conservative).
3. Benchmark: every run is compared against buy-and-hold of the same
   asset. A strategy that can't beat doing nothing isn't a strategy.

Usage:
    python backtest.py --csv data/orcl-1995-2014.txt --strategy sma
    python backtest.py --symbol SPY --start 2000-01-01          # needs internet
    python backtest.py --symbol SPY --strategy tsmom --lookback 126
"""
from __future__ import annotations

import argparse

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from data_loader import load_prices
from strategies import REGISTRY

TRADING_DAYS = 252


# ----------------------------------------------------------------- engine
def run_backtest(prices: pd.DataFrame, strategy, cost_bps: float = 5.0) -> pd.DataFrame:
    if getattr(strategy, "cross_sectional", False):
        raise ValueError("Use portfolio_backtest.py for cross-sectional strategies and a multi-asset universe")
    signal = strategy.signals(prices).fillna(0).astype(float)
    position = signal.shift(1).fillna(0.0)  # trade the day AFTER the signal
    asset_ret = prices["close"].pct_change().fillna(0.0)
    turnover = position.diff().abs().fillna(position.iloc[0])
    strat_ret = position * asset_ret - turnover * (cost_bps / 1e4)

    res = pd.DataFrame(
        {
            "close": prices["close"],
            "position": position,
            "asset_ret": asset_ret,
            "strat_ret": strat_ret,
        }
    )
    res["equity"] = (1 + res["strat_ret"]).cumprod()
    res["bh_equity"] = (1 + res["asset_ret"]).cumprod()
    return res


# ---------------------------------------------------------------- metrics
def _max_drawdown(equity: pd.Series) -> float:
    return float((equity / equity.cummax() - 1.0).min())


def _sharpe(returns: pd.Series) -> float:
    sd = returns.std()
    return float(np.sqrt(TRADING_DAYS) * returns.mean() / sd) if sd > 0 else float("nan")


def _trade_stats(res: pd.DataFrame) -> tuple[int, float]:
    """Number of round trips and fraction that were profitable (net).
    A "trade" is any contiguous block of nonzero exposure (works for
    binary and fractional position strategies alike)."""
    completed, growth = [], None
    for position, ret in zip(res["position"], res["strat_ret"]):
        if position > 0 and growth is None:
            growth = 1.0
        if growth is not None:
            growth *= 1 + ret
            if position == 0:  # include the exit row's transaction cost
                completed.append(growth - 1)
                growth = None
    return len(completed), float(np.mean(np.array(completed) > 0)) if completed else float("nan")


def summarize(res: pd.DataFrame, label: str) -> pd.DataFrame:
    years = len(res) / TRADING_DAYS
    n_trades, win_rate = _trade_stats(res)

    def side(equity: pd.Series, returns: pd.Series, in_market: float, trades, wins):
        return {
            "total_return": equity.iloc[-1] - 1,
            "cagr": equity.iloc[-1] ** (1 / years) - 1,
            "sharpe": _sharpe(returns),
            "max_drawdown": _max_drawdown(equity),
            "time_in_market": in_market,
            "round_trips": trades,
            "win_rate": wins,
        }

    table = pd.DataFrame(
        {
            label: side(res["equity"], res["strat_ret"], res["position"].mean(),
                        n_trades, win_rate),
            "buy_and_hold": side(res["bh_equity"], res["asset_ret"], 1.0,
                                 0, float("nan")),
        }
    )
    return table


def format_table(table: pd.DataFrame) -> str:
    pct = {"total_return", "cagr", "max_drawdown", "time_in_market", "win_rate"}
    out = table.copy().astype(object)
    for row in out.index:
        for col in out.columns:
            v = table.loc[row, col]
            out.loc[row, col] = (
                f"{v:.1%}" if row in pct else (f"{v:.2f}" if isinstance(v, float) else v)
            )
    return out.to_string()


# ------------------------------------------------------------------- plot
def plot_equity(res: pd.DataFrame, title: str, path: str) -> None:
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(10, 7), sharex=True, height_ratios=[3, 1]
    )
    ax1.plot(res.index, res["equity"], label="strategy", lw=1.4)
    ax1.plot(res.index, res["bh_equity"], label="buy & hold", lw=1.4, alpha=0.8)
    ax1.set_yscale("log")
    ax1.set_ylabel("growth of $1 (log)")
    ax1.legend()
    ax1.set_title(title)

    dd = res["equity"] / res["equity"].cummax() - 1
    ax2.fill_between(res.index, dd, 0, alpha=0.4)
    ax2.set_ylabel("drawdown")

    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


# -------------------------------------------------------------------- cli
def main() -> None:
    p = argparse.ArgumentParser(description="Daily backtest runner")
    p.add_argument("--symbol", default="SPY")
    p.add_argument("--start", default="2000-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--csv", default=None, help="local CSV instead of yfinance")
    p.add_argument("--strategy", default="sma", choices=sorted(REGISTRY))
    p.add_argument("--fast", type=int, default=20)
    p.add_argument("--slow", type=int, default=50)
    p.add_argument("--lookback", type=int, default=126)
    p.add_argument("--drop", type=float, default=7.0, help="dip entry: %% below N-day high")
    p.add_argument("--high-lookback", type=int, default=20)
    p.add_argument("--target", type=float, default=10.0, help="take-profit %%")
    p.add_argument("--stop", type=float, default=5.0, help="stop-loss %%")
    p.add_argument("--max-hold", type=int, default=3, help="max trading days per trade")
    p.add_argument("--regime", type=int, default=0,
                   help="dip only: require close > N-day SMA to enter (0 = off)")
    p.add_argument("--cost-bps", type=float, default=5.0)
    p.add_argument("--outdir", default="output")
    args = p.parse_args()

    prices = load_prices(args.symbol, args.start, args.end, csv_path=args.csv)
    if args.strategy == "sma":
        strat = REGISTRY["sma"](args.fast, args.slow)
    elif args.strategy == "tsmom":
        strat = REGISTRY["tsmom"](args.lookback)
    elif args.strategy == "dip":
        strat = REGISTRY["dip"](
            drop=args.drop / 100, lookback=args.high_lookback,
            target=args.target / 100, stop=args.stop / 100,
            max_hold=args.max_hold,
            regime=args.regime if args.regime > 0 else None,
        )
    else:
        strat = REGISTRY[args.strategy]()

    if getattr(strat, "cross_sectional", False):
        p.error("Use portfolio_backtest.py --strategy " + args.strategy + " --symbols MU TSM FCX (or multiple --csv paths)")

    res = run_backtest(prices, strat, cost_bps=args.cost_bps)
    label = f"{strat.name}"
    from pathlib import Path
    Path(args.outdir).mkdir(parents=True, exist_ok=True)

    tag = Path(args.csv).stem.split("-")[0] if args.csv else args.symbol
    src = args.csv or args.symbol
    span = f"{res.index[0]:%Y-%m-%d} → {res.index[-1]:%Y-%m-%d}"

    print(f"\n{src} | {span} | {len(res)} bars | cost {args.cost_bps} bps/side\n")
    print(format_table(summarize(res, label)))

    if getattr(strat, "trade_log", None):
        reasons = pd.Series([t["reason"] for t in strat.trade_log])
        counts = reasons.value_counts()
        pretty = ", ".join(f"{k}: {v} ({v / len(reasons):.0%})" for k, v in counts.items())
        print(f"\nexit reasons: {pretty}")

    png = f"{args.outdir}/equity_{tag}_{strat.name}.png"
    plot_equity(res, f"{strat.name} vs buy & hold — {tag} ({span})", png)
    res.to_csv(f"{args.outdir}/results_{tag}_{strat.name}.csv")
    print(f"\nsaved: {png}")


if __name__ == "__main__":
    main()
