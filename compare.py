"""Run the full strategy lineup across multiple assets. Prints per-asset
metric tables, a Sharpe matrix, and saves a heatmap + CSVs.

Usage:
  python compare.py --csv data/orcl-1995-2014.txt data/nvda-1999-2014.txt
  python compare.py --symbols SPY MU AMD --start 2005-01-01   # needs internet
"""
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from backtest import run_backtest, summarize
from data_loader import load_prices
from strategies import (
    BollingerReversion,
    DipBuyer,
    DonchianBreakout,
    HighProximity,
    MACDCross,
    RSIMeanReversion,
    SMACrossover,
    TimeSeriesMomentum,
    VolTargetMomentum,
)

# MLStrategy is deliberately NOT in this lineup: the exported model was
# trained on this same history, so an in-sample matrix would flatter it.
# Its only honest evaluation is the walk-forward in ml_train.py.
LINEUP = [
    ("SMA 20/50",         lambda: SMACrossover(20, 50)),
    ("SMA 50/200",        lambda: SMACrossover(50, 200)),
    ("TSMOM 6mo",         lambda: TimeSeriesMomentum(126)),
    ("Dip 7/+10/-5/3d",   lambda: DipBuyer(0.07, 20, 0.10, 0.05, 3)),
    ("Dip + 200d filter", lambda: DipBuyer(0.07, 20, 0.10, 0.05, 3, regime=200)),
    ("RSI2<10 + 200d",    lambda: RSIMeanReversion()),
    ("Donchian 55/20",    lambda: DonchianBreakout()),
    ("MACD 12/26/9",      lambda: MACDCross()),
    ("Boll 20/2 + 200d",  lambda: BollingerReversion()),
    ("52wk-high 5%",      lambda: HighProximity()),
    ("TSMOM vol-tgt 15%", lambda: VolTargetMomentum()),
]

SHOW = ["cagr", "sharpe", "max_drawdown", "time_in_market", "round_trips", "win_rate"]
PCT = {"total_return", "cagr", "max_drawdown", "time_in_market", "win_rate"}


def evaluate(prices: pd.DataFrame, cost_bps: float) -> pd.DataFrame:
    rows, bh = {}, None
    for label, make in LINEUP:
        table = summarize(run_backtest(prices, make(), cost_bps=cost_bps), "s")
        rows[label] = table["s"]
        bh = table["buy_and_hold"]
    rows["Buy & hold"] = bh
    return pd.DataFrame(rows).T


def pretty(table: pd.DataFrame) -> str:
    out = table[SHOW].copy().astype(object)
    for col in SHOW:
        for row in out.index:
            v = table.loc[row, col]
            out.loc[row, col] = (
                f"{v:.1%}" if col in PCT
                else f"{v:.0f}" if col == "round_trips"
                else f"{v:.2f}"
            )
    return out.to_string()


def main() -> None:
    ap = argparse.ArgumentParser(description="Strategy x asset comparison")
    ap.add_argument("--csv", nargs="*", default=None)
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--start", default="2000-01-01")
    ap.add_argument("--cost-bps", type=float, default=5.0)
    ap.add_argument("--outdir", default="output")
    args = ap.parse_args()

    assets: dict[str, pd.DataFrame] = {}
    for path in args.csv or []:
        assets[Path(path).stem.split("-")[0].upper()] = load_prices(csv_path=path)
    for sym in args.symbols or []:
        assets[sym.upper()] = load_prices(sym, args.start)
    if not assets:
        ap.error("provide --csv files and/or --symbols")

    sharpe, excess = {}, {}
    for tag, px in assets.items():
        table = evaluate(px, args.cost_bps)
        sharpe[tag] = table["sharpe"]
        excess[tag] = table["cagr"] - table.loc["Buy & hold", "cagr"]
        print(f"\n=== {tag} | {px.index[0]:%Y-%m-%d} -> {px.index[-1]:%Y-%m-%d} "
              f"| {len(px)} bars ===")
        print(pretty(table))

    S, E = pd.DataFrame(sharpe), pd.DataFrame(excess)
    S["MEAN"] = S.mean(axis=1)
    print("\n=== Sharpe: strategy x asset ===")
    print(S.round(2).to_string())
    S.to_csv(f"{args.outdir}/matrix_sharpe.csv")
    E.to_csv(f"{args.outdir}/matrix_excess_cagr.csv")

    data = S.drop(columns=["MEAN"])
    fig, ax = plt.subplots(
        figsize=(2.2 + 1.4 * len(data.columns), 0.6 * len(data) + 1.6)
    )
    im = ax.imshow(data.to_numpy(dtype=float), cmap="RdYlGn",
                   vmin=-0.6, vmax=1.0, aspect="auto")
    ax.set_xticks(range(len(data.columns)), data.columns)
    ax.set_yticks(range(len(data.index)), data.index)
    for i in range(data.shape[0]):
        for j in range(data.shape[1]):
            ax.text(j, i, f"{data.iat[i, j]:.2f}", ha="center", va="center",
                    fontsize=9)
    ax.set_title(f"Sharpe ratio by strategy x asset ({args.cost_bps:.0f} bps/side)")
    fig.colorbar(im, ax=ax, shrink=0.85)
    fig.tight_layout()
    png = f"{args.outdir}/strategy_matrix.png"
    fig.savefig(png, dpi=140)
    print(f"\nsaved: {png}")


if __name__ == "__main__":
    main()
