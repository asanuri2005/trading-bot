"""Walk-forward training and evaluation for the version-2 ML books.

Both models answer the same question: will this stock beat the median
watchlist stock over the next `horizon` sessions? Both use the same
PANEL_FEATURES (strategies.py) and the same walk-forward referee:

  * quarterly refits, each trained only on rows whose label had fully
    resolved before the quarter began, then scored on that quarter
  * logistic regression standardizes with the training window's statistics
  * LightGBM hyperparameters are fixed below before any result is seen;
    tuning them on these results would turn the evaluation into a fit

Logistic regression is the baseline. The trees are worth keeping only if
they beat it out of sample, and both are worth something only if they beat
holding the whole watchlist.

Evaluation trades the stitched out-of-sample predictions the way the live
MLRanker does: average each stock's probability over the last `smooth`
sessions, hold the top `top_n` equally, rebalance at the close, earn the
next session's move, pay `cost_bps` per side on turnover.

Usage:
  python ml_rank.py --symbols TSM COP SCCO ... --start 2005-01-01
  python ml_rank.py --watchlist watchlist.txt --start 2005-01-01 --export
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from data_loader import load_prices
from ml_train import fit_logreg
from strategies import PANEL_FEATURES, LogisticModel, TreeEnsembleModel, compute_panel_features

# Fixed in advance. Shallow trees, many samples per leaf, slow learning and
# row/column subsampling: financial data is small and noisy, so the trees
# must be kept from memorizing it.
GBT_PARAMS = dict(n_estimators=300, learning_rate=0.03, num_leaves=7, max_depth=3,
                  min_child_samples=500, subsample=0.8, subsample_freq=1,
                  colsample_bytree=0.8, reg_lambda=1.0, random_state=7, verbose=-1)


# ------------------------------------------------------------------ data
def load_universe(symbols: list[str], start: str) -> dict[str, pd.DataFrame]:
    prices = {}
    for symbol in symbols:
        try:
            prices[symbol] = load_prices(symbol, start)
        except Exception as exc:  # report and continue; the universe shrinks visibly
            print(f"  skipped {symbol}: {exc}")
    return prices


def build_dataset(prices: dict[str, pd.DataFrame], horizon: int) -> pd.DataFrame:
    """Panel features plus each row's relative label and the date it resolves."""
    panel = compute_panel_features(prices)
    labels = []
    for symbol, px in prices.items():
        close = px["close"]
        fwd = close.shift(-horizon) / close - 1
        resolved = pd.Series(close.index, index=close.index).shift(-horizon)
        labels.append(pd.DataFrame({"fwd": fwd, "label_date": resolved, "symbol": symbol},
                                   index=close.index.rename("date")))
    labels = pd.concat(labels).reset_index().set_index(["date", "symbol"])
    data = panel.join(labels, how="left")
    # Beat the peers: compare with the median forward return of the stocks
    # that have one on the same date.
    median = data.groupby(level="date")["fwd"].transform("median")
    data["y"] = (data["fwd"] > median).astype(float).where(data["fwd"].notna())
    return data


# ---------------------------------------------------------------- models
def fit_logistic(X: np.ndarray, y: np.ndarray) -> dict:
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd[sd == 0] = 1.0
    w, b = fit_logreg((X - mu) / sd, y, l2=1.0)
    return dict(type="logistic_regression", mu=mu.tolist(), sigma=sd.tolist(),
                coef=w.tolist(), intercept=float(b))


def fit_trees(X: np.ndarray, y: np.ndarray) -> tuple[dict, object]:
    import lightgbm as lgb

    model = lgb.LGBMClassifier(**GBT_PARAMS).fit(X, y)
    return export_trees(model.booster_), model


def export_trees(booster) -> dict:
    """Flatten LightGBM's tree dump into the node arrays TreeEnsembleModel reads."""
    trees = []
    for info in booster.dump_model()["tree_info"]:
        nodes = dict(feature=[], threshold=[], left=[], right=[], value=[])

        def visit(node):
            i = len(nodes["feature"])
            for k in nodes:
                nodes[k].append(None)
            if "leaf_value" in node:
                nodes["feature"][i], nodes["threshold"][i] = -1, 0.0
                nodes["left"][i] = nodes["right"][i] = -1
                nodes["value"][i] = float(node["leaf_value"])
                return i
            if node.get("decision_type") != "<=" or node.get("missing_type") == "Zero":
                raise ValueError(f"unsupported split {node.get('decision_type')}/{node.get('missing_type')}")
            nodes["feature"][i] = int(node["split_feature"])
            nodes["threshold"][i] = float(node["threshold"])
            nodes["value"][i] = 0.0
            nodes["left"][i] = visit(node["left_child"])
            nodes["right"][i] = visit(node["right_child"])
            return i

        visit(info["tree_structure"])
        trees.append(nodes)
    return dict(type="gradient_boosted_trees", trees=trees)


def predict(model: dict, X: np.ndarray) -> np.ndarray:
    return (TreeEnsembleModel if model["type"] == "gradient_boosted_trees" else LogisticModel)(model).predict(X)


# ----------------------------------------------------------- walk-forward
def walk_forward(data: pd.DataFrame, oos_start: str, min_train: int) -> pd.DataFrame:
    """Out-of-sample probabilities from both models, refit every quarter."""
    dates = data.index.get_level_values("date")
    cutoffs = list(pd.date_range(oos_start, dates.max(), freq="QS"))
    labeled = data.dropna(subset=["y", "label_date"])
    chunks = []
    for i, cut in enumerate(cutoffs):
        nxt = cutoffs[i + 1] if i + 1 < len(cutoffs) else dates.max() + pd.Timedelta(days=1)
        train = labeled[labeled["label_date"] < cut]
        test = data[(dates >= cut) & (dates < nxt)]
        if len(train) < min_train or test.empty:
            continue
        Xtr, ytr = train[PANEL_FEATURES].to_numpy(float), train["y"].to_numpy(float)
        Xte = test[PANEL_FEATURES].to_numpy(float)
        logistic = fit_logistic(Xtr, ytr)
        trees, _ = fit_trees(Xtr, ytr)
        chunk = test[["fwd", "y"]].copy()
        chunk["p_logreg"] = predict(logistic, Xte)
        chunk["p_gbt"] = predict(trees, Xte)
        chunks.append(chunk)
        print(f"  fit {len(chunks):>2}: train rows {len(train):>7,} -> {cut:%Y-%m-%d} scored {len(test):>5,}")
    if not chunks:
        raise ValueError("No eligible walk-forward windows; lower --min-train or start earlier")
    return pd.concat(chunks).sort_index()


# ------------------------------------------------------------- evaluation
def rank_skill(oos: pd.DataFrame, column: str) -> dict:
    """Daily rank correlation with realized forward returns, and pick accuracy."""
    scored = oos.dropna(subset=["fwd"])
    ic = scored.groupby(level="date").apply(
        lambda d: d[column].rank().corr(d["fwd"].rank()) if len(d) >= 5 else np.nan).dropna()
    return dict(ic_mean=float(ic.mean()), ic_tstat=float(ic.mean() / ic.std() * np.sqrt(len(ic))),
                ic_positive_days=float((ic > 0).mean()))


def portfolio(oos: pd.DataFrame, returns: pd.DataFrame, picks: pd.DataFrame, cost_bps: float) -> dict:
    """Equal weight across each day's picks; earn the next session; pay turnover."""
    weights = picks.div(picks.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
    turnover = weights.diff().abs().sum(axis=1).fillna(weights.abs().sum(axis=1))
    daily = (weights * returns.shift(-1).reindex(weights.index)).sum(axis=1) - turnover * cost_bps / 1e4
    daily = daily.iloc[:-1]  # the last day has no next-session return yet
    equity = (1 + daily).cumprod()
    years = len(daily) / 252
    return dict(cagr=float(equity.iloc[-1] ** (1 / years) - 1),
                sharpe=float(daily.mean() / daily.std() * np.sqrt(252)),
                max_drawdown=float((equity / equity.cummax() - 1).min()),
                turnover_per_year=float(turnover.mean() * 252), daily=daily)


def evaluate(oos: pd.DataFrame, prices: dict[str, pd.DataFrame], top_n: int, smooth: int,
             cost_bps: float) -> tuple[pd.DataFrame, dict]:
    close = pd.concat({s: px["close"] for s, px in prices.items()}, axis=1).sort_index()
    window = close.loc[oos.index.get_level_values("date").min():]
    returns = window.pct_change(fill_method=None)
    eligible = oos["p_logreg"].unstack("symbol").reindex(window.index).notna()

    def top(scores: pd.DataFrame) -> pd.DataFrame:
        ranks = scores.where(eligible).rank(axis=1, ascending=False, method="first")
        return (ranks <= top_n).astype(float)

    results = {}
    for name, column in (("ML logreg", "p_logreg"), ("ML trees (GBT)", "p_gbt")):
        p = oos[column].unstack("symbol").reindex(window.index)
        smoothed = p.rolling(smooth, min_periods=1).mean()
        results[name] = dict(**portfolio(oos, returns, top(smoothed), cost_bps), **rank_skill(oos, column))
    r126 = window.pct_change(126, fill_method=None)
    results["Rotation top-3 (no ML)"] = portfolio(oos, returns, (r126.where(eligible).rank(
        axis=1, ascending=False, method="first") <= 3).astype(float), cost_bps)
    results["Hold all, equal weight"] = portfolio(oos, returns, eligible.astype(float), cost_bps)
    table = pd.DataFrame({k: {m: v for m, v in r.items() if m != "daily"} for k, r in results.items()}).T
    return table, results


# ------------------------------------------------------------------ main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="*", default=[])
    ap.add_argument("--watchlist", help="file with one ticker per line; '#' comments allowed")
    ap.add_argument("--start", default="2005-01-01")
    ap.add_argument("--oos-start", default="2012-01-01")
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--top-n", type=int, default=5)
    ap.add_argument("--smooth", type=int, default=5)
    ap.add_argument("--min-train", type=int, default=20000)
    ap.add_argument("--cost-bps", type=float, default=5.0)
    ap.add_argument("--outdir", default="output/ml_rank")
    ap.add_argument("--export", action="store_true", help="write models/ml_logreg_v2.json and models/ml_gbt.json")
    args = ap.parse_args()

    symbols = [s.upper() for s in args.symbols]
    if args.watchlist:
        for line in Path(args.watchlist).read_text().splitlines():
            line = line.split("#")[0].strip().upper()
            if line:
                symbols.append(line)
    symbols = sorted(set(symbols))
    if len(symbols) < 6:
        ap.error("need at least 6 symbols for a cross-sectional ranking")
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)

    print(f"loading {len(symbols)} symbols from {args.start}...")
    prices = load_universe(symbols, args.start)
    data = build_dataset(prices, args.horizon)
    print(f"dataset: {len(data):,} rows, {data['y'].notna().sum():,} labeled, "
          f"{data.index.get_level_values('date').min():%Y-%m-%d} to {data.index.get_level_values('date').max():%Y-%m-%d}")

    print("walk-forward:")
    oos = walk_forward(data, args.oos_start, args.min_train)
    table, results = evaluate(oos, prices, args.top_n, args.smooth, args.cost_bps)
    pretty = table.copy()
    for c in ("cagr", "max_drawdown", "ic_positive_days"):
        pretty[c] = pretty[c].map(lambda v: f"{v:.1%}" if pd.notna(v) else "")
    for c in ("sharpe", "ic_tstat", "turnover_per_year"):
        pretty[c] = pretty[c].map(lambda v: f"{v:.2f}" if pd.notna(v) else "")
    pretty["ic_mean"] = pretty["ic_mean"].map(lambda v: f"{v:.4f}" if pd.notna(v) else "")
    span = oos.index.get_level_values("date")
    print(f"\nout-of-sample {span.min():%Y-%m-%d} to {span.max():%Y-%m-%d}, top {args.top_n}, "
          f"smooth {args.smooth}, {args.cost_bps:g} bps per side:\n{pretty.to_string()}")
    table.to_csv(out / "evaluation.csv")
    oos.to_csv(out / "oos_predictions.csv")

    if not args.export:
        return
    labeled = data.dropna(subset=["y", "label_date"])
    X, y = labeled[PANEL_FEATURES].to_numpy(float), labeled["y"].to_numpy(float)
    trees, fitted = fit_trees(X, y)
    # The numpy evaluator must reproduce LightGBM exactly, or the live book
    # would trade a different model from the one evaluated here.
    gap = np.abs(predict(trees, X[:5000]) - fitted.predict_proba(X[:5000])[:, 1]).max()
    if gap > 1e-9:
        raise ValueError(f"numpy tree evaluation differs from LightGBM by {gap}")
    import lightgbm

    common = dict(features=PANEL_FEATURES, label=f"beats_peer_median_fwd_{args.horizon}d",
                  trained_on=sorted(prices), trained_through=str(labeled.index.get_level_values("date").max().date()),
                  labels_resolved_through=str(labeled["label_date"].max().date()),
                  training_args=vars(args), exported_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                  feature_code_sha256=hashlib.sha256(Path("strategies.py").read_bytes()).hexdigest())
    for filename, model, label in (("ml_logreg_v2.json", fit_logistic(X, y), "ML logreg"),
                                   ("ml_gbt.json", trees, "ML trees (GBT)")):
        model.update(common, walk_forward_result={k: v for k, v in results[label].items() if k != "daily"})
        if model["type"] == "gradient_boosted_trees":
            model.update(params=GBT_PARAMS, lightgbm_version=lightgbm.__version__)
        Path("models").mkdir(exist_ok=True)
        Path("models", filename).write_text(json.dumps(model, indent=None, separators=(",", ":")))
        print(f"exported models/{filename} ({Path('models', filename).stat().st_size / 1024:.0f} KB)")
    print("Only the walk-forward numbers above are evidence; the exported models have seen all of it.")


if __name__ == "__main__":
    main()
