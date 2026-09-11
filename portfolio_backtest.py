"""Multi-asset, cash-inclusive backtest using the same shadow book engine.

Signal at t-1 -> rebalance at t close -> earn t to t+1 move. All strategies
share dates, initial cash and transaction cost convention. B&H buys once.
"""
import argparse
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent / "aws" / "src"))
from shared.portfolio import advance, weights_for
from data_loader import load_prices
from strategies import REGISTRY


def run_portfolio(prices, strategy, budget=12000, cost_bps=5, start=None):
    close = pd.concat({s: px["close"] for s, px in prices.items()}, axis=1).dropna().sort_index()
    if close.empty or not close.index.is_unique or (close <= 0).any().any():
        raise ValueError("invalid aligned price history")
    frames = {s: close[[s]].rename(columns={s: "close"}) for s in close}
    dates = close.index[210:]
    if start:
        dates = dates[dates >= pd.Timestamp(start)]
    if not len(dates):
        raise ValueError("need at least 211 common bars and an evaluation window")
    # Independent strategies can compute their backward-looking signals once.
    signals = None if getattr(strategy, "cross_sectional", False) else {
        s: strategy.signals(px) for s, px in frames.items()}
    previous, results = None, []
    for date in dates:
        i = close.index.get_loc(date)
        if signals is None:
            weights = weights_for(strategy, {s: px.iloc[:i] for s, px in frames.items()})
        else:
            weights = {s: float(sig.iloc[i-1]) / len(frames) for s, sig in signals.items()}
        previous = advance(previous, close.loc[date].to_dict(), weights, budget, cost_bps,
                           hold=getattr(strategy, "buy_and_hold", False))
        results.append(dict(date=date, **{k: v for k, v in previous.items() if k not in ("positions", "prices")}))
    return pd.DataFrame(results).set_index("date")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", nargs="*")
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--start", default="2000-01-01")
    parser.add_argument("--eval-start")
    parser.add_argument("--strategy", choices=sorted(REGISTRY), default="xsmom")
    parser.add_argument("--budget", type=float, default=12000)
    parser.add_argument("--cost-bps", type=float, default=5)
    parser.add_argument("--outdir", default="output")
    args = parser.parse_args()
    assets = {Path(p).stem: load_prices(csv_path=p) for p in args.csv or []}
    assets.update({s: load_prices(s, args.start) for s in args.symbols or []})
    if not assets:
        parser.error("provide --csv or --symbols")
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    for key in dict.fromkeys([args.strategy, "buyhold"]):
        strategy = REGISTRY[key]()
        result = run_portfolio(assets, strategy, args.budget, args.cost_bps, args.eval_start)
        path = out / f"portfolio_{strategy.name}.csv"
        result.to_csv(path)
        returns = result.day_ret
        sharpe = returns.mean() / returns.std() * 252**0.5 if returns.std() else float("nan")
        drawdown = (result.equity / result.equity.cummax().clip(lower=args.budget) - 1).min()
        print(f"{strategy.name}: return={result.cum_ret.iloc[-1]:.2%}, Sharpe={sharpe:.2f}, maxDD={drawdown:.2%}; {path}")


if __name__ == "__main__":
    main()
