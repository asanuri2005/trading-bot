"""Walk-forward ML signal research + model export.

Trains a pure-numpy logistic regression to predict next-`horizon`-day
direction from technical features, evaluated the only honest way:
**walk-forward** — fit strictly on the past, predict the next quarter,
roll, stitch the out-of-sample predictions together, and only judge those.

Leakage protections:
  * features are strictly backward-looking (see strategies.compute_features)
  * a training row is only used once its label has fully RESOLVED before
    the cutoff (no peeking at in-flight forward returns)
  * standardization stats come from the training window only
  * the OOS signal still goes through the engine's one-day execution shift

The exported JSON model is consumed by strategies.MLStrategy — same
feature code, same math, zero new runtime dependencies.

Usage:
  python ml_train.py --csv data/orcl-1995-2014.txt data/nvda-1999-2014.txt data/yhoo-1996-2015.txt
  python ml_train.py --symbols SPY MU --start 2005-01-01     # needs internet
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import hashlib
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from backtest import run_backtest, summarize
from data_loader import load_prices
from strategies import FEATURES, TimeSeriesMomentum, compute_features


# ------------------------------------------------------------- plumbing
class PrecomputedSignals:
    """Adapter: feed an externally computed 0/1 series into the engine."""

    def __init__(self, series: pd.Series, name: str):
        self._series, self.name = series, name

    def signals(self, prices: pd.DataFrame) -> pd.Series:
        return self._series.reindex(prices.index).fillna(0).astype(int)


def sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def fit_logreg(X: np.ndarray, y: np.ndarray, l2: float = 1.0,
               lr: float = 0.5, iters: int = 800) -> tuple[np.ndarray, float]:
    """Full-batch gradient descent on L2-regularized logistic loss."""
    n, d = X.shape
    w = np.zeros(d)
    base = float(np.clip(y.mean(), 1e-6, 1 - 1e-6))
    b = float(np.log(base / (1 - base)))
    for _ in range(iters):
        p = sigmoid(X @ w + b)
        w -= lr * (X.T @ (p - y) / n + l2 * w / n)
        b -= lr * float((p - y).mean())
    return w, b


def build_rows(prices: pd.DataFrame, symbol: str, horizon: int) -> pd.DataFrame:
    """Feature rows with labels; keeps only rows whose label has resolved."""
    feats = compute_features(prices)
    close = prices["close"]
    fwd = close.shift(-horizon) / close - 1
    rows = feats.copy()
    rows["y"] = (fwd > 0).where(fwd.notna()).astype(float)
    # the calendar date on which this row's label becomes knowable:
    rows["label_date"] = pd.Series(close.index, index=close.index).shift(-horizon)
    rows["symbol"] = symbol
    rows["date"] = rows.index
    return rows.dropna(subset=FEATURES + ["y", "label_date"]).reset_index(drop=True)


def walk_forward(pooled: pd.DataFrame, oos_start: str, l2: float,
                 min_train: int) -> pd.DataFrame:
    """Quarterly refits; returns OOS rows with predicted probabilities."""
    end = pooled["date"].max()
    cutoffs = list(pd.date_range(oos_start, end, freq="QS"))
    out, fits = [], 0
    for i, cut in enumerate(cutoffs):
        nxt = cutoffs[i + 1] if i + 1 < len(cutoffs) else end + pd.Timedelta(days=1)
        train = pooled[pooled["label_date"] < cut]
        test = pooled[(pooled["date"] >= cut) & (pooled["date"] < nxt)]
        if len(train) < min_train or test.empty:
            continue
        Xtr = train[FEATURES].to_numpy(dtype=float)
        mu, sd = Xtr.mean(axis=0), Xtr.std(axis=0)
        sd[sd == 0] = 1.0
        w, b = fit_logreg((Xtr - mu) / sd, train["y"].to_numpy(dtype=float), l2=l2)
        Xte = (test[FEATURES].to_numpy(dtype=float) - mu) / sd
        chunk = test[["symbol", "date", "y"]].copy()
        chunk["prob"] = sigmoid(Xte @ w + b)
        out.append(chunk)
        fits += 1
    print(f"walk-forward: {fits} quarterly fits, "
          f"{sum(len(c) for c in out)} out-of-sample predictions")
    if not out:
        raise ValueError("No eligible walk-forward windows; provide more training history or lower --min-train")
    return pd.concat(out, ignore_index=True)


def evaluate_oos(prices, predictions, threshold=0.5, cost_bps=5.0):
    """Equal evaluation dates and pre-window feature history for both models."""
    predictions = predictions.sort_index()
    window = prices.loc[predictions.index.min():predictions.index.max()]
    probabilities = predictions.reindex(window.index)
    if probabilities.isna().any():
        raise ValueError("Missing OOS prediction inside evaluation window")
    ml_signal = (probabilities > threshold).astype(int)
    trend_signal = TimeSeriesMomentum(126).signals(prices).reindex(window.index)
    return (run_backtest(window, PrecomputedSignals(ml_signal, "ml_logreg"), cost_bps),
            run_backtest(window, PrecomputedSignals(trend_signal, "tsmom_126"), cost_bps))


# ----------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description="Walk-forward ML training")
    ap.add_argument("--csv", nargs="*", default=None)
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--start", default="2000-01-01")
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--l2", type=float, default=1.0)
    ap.add_argument("--oos-start", default="2001-01-01")
    ap.add_argument("--min-train", type=int, default=750)
    ap.add_argument("--cost-bps", type=float, default=5.0)
    ap.add_argument("--out", default="models/candidates/logreg.json")
    ap.add_argument("--outdir", default="output")
    args = ap.parse_args()
    Path(args.outdir).mkdir(parents=True, exist_ok=True)

    assets: dict[str, pd.DataFrame] = {}
    for path in args.csv or []:
        assets[Path(path).stem.split("-")[0].upper()] = load_prices(csv_path=path)
    for sym in args.symbols or []:
        assets[sym.upper()] = load_prices(sym, args.start)
    if not assets:
        ap.error("provide --csv files and/or --symbols")

    pooled = pd.concat(
        [build_rows(px, tag, args.horizon) for tag, px in assets.items()],
        ignore_index=True,
    ).sort_values("date")
    base_rate = pooled["y"].mean()
    print(f"pooled samples: {len(pooled)} across {len(assets)} assets | "
          f"base rate P(up over {args.horizon}d) = {base_rate:.3f}")

    oos = walk_forward(pooled, args.oos_start, args.l2, args.min_train)
    acc = float(((oos["prob"] > args.threshold) == (oos["y"] > 0)).mean())
    naive = float(oos["y"].mean())
    print(f"OOS direction accuracy: {acc:.3f} vs always-up baseline {naive:.3f}\n")

    # ------- trade the OOS probabilities through the real engine
    fig, axes = plt.subplots(1, len(assets), figsize=(5.2 * len(assets), 4),
                             squeeze=False)
    sharpe_rows = {}
    for j, (tag, px) in enumerate(assets.items()):
        sub = oos[oos["symbol"] == tag].set_index("date").sort_index()
        if sub.empty:
            continue
        ml_res, ts_res = evaluate_oos(px, sub["prob"], args.threshold, args.cost_bps)
        window = px.loc[ml_res.index]
        ml_t = summarize(ml_res, "ml")
        ts_t = summarize(ts_res, "tsmom")
        table = pd.concat([ml_t["ml"], ts_t["tsmom"], ml_t["buy_and_hold"]],
                          axis=1, keys=["ML logreg", "TSMOM 6mo", "Buy&hold"])
        sharpe_rows[tag] = table.loc["sharpe"]
        print(f"=== {tag} | OOS {window.index[0]:%Y-%m-%d} -> "
              f"{window.index[-1]:%Y-%m-%d} ===")
        pretty = table.copy().astype(object)
        for r in ("total_return", "cagr", "max_drawdown", "time_in_market", "win_rate"):
            pretty.loc[r] = [f"{v:.1%}" for v in table.loc[r]]
        pretty.loc["sharpe"] = [f"{v:.2f}" for v in table.loc["sharpe"]]
        pretty.loc["round_trips"] = [f"{v:.0f}" for v in table.loc["round_trips"]]
        print(pretty.to_string(), "\n")

        ax = axes[0][j]
        ax.plot(ml_res.index, ml_res["equity"], label="ML", lw=1.3)
        ax.plot(ts_res.index, ts_res["equity"], label="TSMOM", lw=1.3)
        ax.plot(ml_res.index, ml_res["bh_equity"], label="B&H", lw=1.1, alpha=0.7)
        ax.set_yscale("log")
        ax.set_title(f"{tag} (out-of-sample)")
        ax.legend(fontsize=8)

    S = pd.DataFrame(sharpe_rows)
    S["MEAN"] = S.mean(axis=1)
    print("=== OOS Sharpe summary ===")
    print(S.round(2).to_string())

    fig.tight_layout()
    png = f"{args.outdir}/ml_walkforward.png"
    fig.savefig(png, dpi=140)
    print(f"\nsaved: {png}")

    # ------- final model on all resolved data -> JSON for MLStrategy
    X = pooled[FEATURES].to_numpy(dtype=float)
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd[sd == 0] = 1.0
    w, b = fit_logreg((X - mu) / sd, pooled["y"].to_numpy(dtype=float), l2=args.l2)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    model = {
        "type": "logistic_regression",
        "features": FEATURES,
        "mu": [round(float(v), 8) for v in mu],
        "sigma": [round(float(v), 8) for v in sd],
        "coef": [round(float(v), 8) for v in w],
        "intercept": round(float(b), 8),
        "threshold": args.threshold,
        "label": f"fwd_{args.horizon}d_up",
        "l2": args.l2,
        "trained_on": sorted(assets),
        "trained_through": str(pooled["date"].max().date()),
        "labels_resolved_through": str(pooled["label_date"].max().date()),
        "training_args": vars(args),
        "feature_code_sha256": hashlib.sha256(Path(__file__).with_name("strategies.py").read_bytes()).hexdigest(),
        "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    with open(args.out, "w") as fh:
        json.dump(model, fh, indent=1)
    oos.to_csv(Path(args.outdir) / "ml_oos_predictions.csv", index=False)
    S.to_csv(Path(args.outdir) / "ml_oos_sharpe.csv")
    weights = sorted(zip(FEATURES, w), key=lambda t: -abs(t[1]))
    print(f"\nexported {args.out} | top weights: "
          + ", ".join(f"{k}={v:+.2f}" for k, v in weights[:4]))
    print("NOTE: the exported model has seen ALL of this history — only the "
          "walk-forward numbers above are evidence. Never judge it on "
          "in-sample backtests.")


if __name__ == "__main__":
    main()
