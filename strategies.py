"""Trading strategies.

Contract: a strategy exposes `.signals(prices) -> pd.Series` of target
positions in {0, 1} (flat / fully long), indexed like `prices`.

To add a strategy: write a class with `.signals()` and register it in
REGISTRY. Nothing else changes — the same classes get imported unchanged
by the AWS Lambda signal generator.
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd


def _sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def _rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder-smoothed RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    return (100 - 100 / (1 + gain / loss.replace(0, np.nan))).fillna(50)


# --------------------------------------------------------------- features
# Shared by ml_train.py (training) and MLStrategy (inference). Order matters:
# the exported model's mu/sigma/coef arrays follow this exact order.
FEATURES = ["r1", "r5", "r21", "r63", "r126",
            "vol21", "rsi2", "rsi14", "dist_sma50", "dist_sma200",
            "vol_ratio", "mom_tstat", "rsi2_x_trend"]


def compute_features(prices: pd.DataFrame) -> pd.DataFrame:
    """Close-only technical features. Strictly backward-looking."""
    c = prices["close"]
    f = pd.DataFrame(index=c.index)
    f["r1"] = c.pct_change()
    f["r5"] = c.pct_change(5)
    f["r21"] = c.pct_change(21)
    f["r63"] = c.pct_change(63)
    f["r126"] = c.pct_change(126)
    f["vol21"] = c.pct_change().rolling(21).std()
    f["rsi2"] = _rsi(c, 2)
    f["rsi14"] = _rsi(c, 14)
    f["dist_sma50"] = c / _sma(c, 50) - 1
    f["dist_sma200"] = c / _sma(c, 200) - 1
    # v2 additions: vol regime shift, risk-adjusted momentum, and the
    # "oversold inside an uptrend" interaction (RSI2 strategy logic as a feature)
    f["vol_ratio"] = f["vol21"] / c.pct_change().rolling(63).std() - 1
    f["mom_tstat"] = f["r21"] / (f["vol21"] * np.sqrt(21)).replace(0, np.nan)
    f["rsi2_x_trend"] = (f["rsi2"] - 50) / 50 * f["dist_sma200"]
    return f[FEATURES]


class SMACrossover:
    """Trend following: long while the fast SMA is above the slow SMA."""

    def __init__(self, fast: int = 20, slow: int = 50):
        if fast >= slow:
            raise ValueError("fast window must be smaller than slow window")
        self.fast, self.slow = fast, slow
        self.name = f"sma_{fast}_{slow}"

    def signals(self, prices: pd.DataFrame) -> pd.Series:
        c = prices["close"]
        return (_sma(c, self.fast) > _sma(c, self.slow)).astype(int)


class TimeSeriesMomentum:
    """Trend following: long while the trailing `lookback`-day return is positive."""

    def __init__(self, lookback: int = 126):
        self.lookback = lookback
        self.name = f"tsmom_{lookback}"

    def signals(self, prices: pd.DataFrame) -> pd.Series:
        return (prices["close"].pct_change(self.lookback) > 0).astype(int)


class DipBuyer:
    """Mean reversion: systematic dip-buying, optional trend (regime) filter.

    Entry:  close at least `drop` below the prior `lookback`-day high, and
            (if `regime` is set) close above its `regime`-day SMA.
    Exit:   first of gain >= `target`, loss <= -`stop`, or `max_hold` days.
    """

    def __init__(self, drop: float = 0.07, lookback: int = 20,
                 target: float = 0.10, stop: float = 0.05, max_hold: int = 3,
                 regime: int | None = None):
        self.drop, self.lookback = drop, lookback
        self.target, self.stop, self.max_hold = target, stop, max_hold
        self.regime = regime
        flt = f"_r{regime}" if regime else ""
        self.name = (f"dip{int(drop * 100)}_t{int(target * 100)}"
                     f"_s{int(stop * 100)}_{max_hold}d{flt}")
        self.trade_log: list[dict] = []

    def signals(self, prices: pd.DataFrame) -> pd.Series:
        close = prices["close"].to_numpy(dtype=float)
        prior_high = (prices["close"].rolling(self.lookback).max()
                      .shift(1).to_numpy(dtype=float))
        if self.regime:
            trend_ok = (prices["close"] > _sma(prices["close"], self.regime)).to_numpy()
        else:
            trend_ok = np.ones(len(close), dtype=bool)
        idx = prices.index
        sig = np.zeros(len(close), dtype=int)
        self.trade_log = []
        in_pos, entry_px, entry_i = False, 0.0, 0

        for t in range(len(close)):
            if not in_pos:
                if (trend_ok[t] and not np.isnan(prior_high[t])
                        and close[t] <= prior_high[t] * (1 - self.drop)):
                    in_pos, entry_px, entry_i = True, close[t], t
                    sig[t] = 1
            else:
                ret = close[t] / entry_px - 1
                held = t - entry_i
                reason = ("target" if ret >= self.target
                          else "stop" if ret <= -self.stop
                          else "time" if held >= self.max_hold
                          else None)
                if reason:
                    in_pos = False
                    self.trade_log.append({
                        "entry_date": idx[entry_i], "exit_date": idx[t],
                        "ret": ret, "days_held": held, "reason": reason,
                    })
                else:
                    sig[t] = 1
        return pd.Series(sig, index=idx)


class RSIMeanReversion:
    """Mean reversion (Connors-style): buy short-term oversold inside an uptrend.

    Entry: close > `regime`-day SMA and RSI(`period`) < `buy_below`.
    Exit:  close > `exit_sma`-day SMA (the bounce arrived).
    """

    def __init__(self, period: int = 2, buy_below: float = 10.0,
                 exit_sma: int = 5, regime: int = 200):
        self.period, self.buy_below = period, buy_below
        self.exit_sma, self.regime = exit_sma, regime
        self.name = f"rsi{period}_lt{int(buy_below)}_r{regime}"

    def signals(self, prices: pd.DataFrame) -> pd.Series:
        c = prices["close"]
        rsi = _rsi(c, self.period)
        enter = ((c > _sma(c, self.regime)) & (rsi < self.buy_below)).to_numpy()
        leave = (c > _sma(c, self.exit_sma)).to_numpy()
        sig = np.zeros(len(c), dtype=int)
        in_pos = False
        for t in range(len(c)):
            if not in_pos and enter[t]:
                in_pos = True
            elif in_pos and leave[t]:
                in_pos = False
            sig[t] = 1 if in_pos else 0
        return pd.Series(sig, index=c.index)


class DonchianBreakout:
    """Trend following (turtle-style): buy strength instead of weakness.

    Entry: close breaks above the prior `entry_n`-day high.
    Exit:  close breaks below the prior `exit_n`-day low.
    """

    def __init__(self, entry_n: int = 55, exit_n: int = 20):
        self.entry_n, self.exit_n = entry_n, exit_n
        self.name = f"donchian_{entry_n}_{exit_n}"

    def signals(self, prices: pd.DataFrame) -> pd.Series:
        c = prices["close"]
        hi = c.rolling(self.entry_n).max().shift(1)
        lo = c.rolling(self.exit_n).min().shift(1)
        enter = (c > hi).to_numpy()
        leave = (c < lo).to_numpy()
        sig = np.zeros(len(c), dtype=int)
        in_pos = False
        for t in range(len(c)):
            if not in_pos and enter[t]:
                in_pos = True
            elif in_pos and leave[t]:
                in_pos = False
            sig[t] = 1 if in_pos else 0
        return pd.Series(sig, index=c.index)


class MLStrategy:
    """Logistic regression over `FEATURES`, trained walk-forward offline by
    ml_train.py and exported to JSON. Inference is a standardized dot
    product in numpy — no new runtime dependencies, Lambda-safe.

    Long when P(next-horizon return > 0) exceeds `threshold`; flat during
    the feature warm-up window or when the model file is absent.
    """

    def __init__(self, model_path: str | None = None,
                 threshold: float | None = None):
        path = model_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "models", "logreg.json")
        with open(path) as fh:
            m = json.load(fh)
        if m.get("features") != FEATURES:
            raise ValueError("model features don't match strategies.FEATURES — retrain")
        self.mu = np.array(m["mu"], dtype=float)
        self.sigma = np.where(np.array(m["sigma"], dtype=float) == 0, 1.0,
                              np.array(m["sigma"], dtype=float))
        self.coef = np.array(m["coef"], dtype=float)
        self.intercept = float(m["intercept"])
        self.threshold = float(threshold if threshold is not None
                               else m.get("threshold", 0.5))
        self.meta = m
        self.name = "ml_logreg"

    def probabilities(self, prices: pd.DataFrame) -> pd.Series:
        X = (compute_features(prices).to_numpy(dtype=float) - self.mu) / self.sigma
        z = X @ self.coef + self.intercept
        valid = ~np.isnan(z)
        p = 1.0 / (1.0 + np.exp(-np.clip(np.where(valid, z, 0.0), -30, 30)))
        return pd.Series(np.where(valid, p, np.nan), index=prices.index)

    def signals(self, prices: pd.DataFrame) -> pd.Series:
        p = self.probabilities(prices)
        return ((p > self.threshold).fillna(False)).astype(int)


class MACDCross:
    """Trend following: long while the MACD line is above its signal line."""

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        self.fast, self.slow, self.signal = fast, slow, signal
        self.name = f"macd_{fast}_{slow}_{signal}"

    def signals(self, prices: pd.DataFrame) -> pd.Series:
        c = prices["close"]
        macd = c.ewm(span=self.fast, adjust=False).mean() - \
            c.ewm(span=self.slow, adjust=False).mean()
        sig_line = macd.ewm(span=self.signal, adjust=False).mean()
        return (macd > sig_line).astype(int)


class BollingerReversion:
    """Mean reversion: buy a close below the lower Bollinger band inside an
    uptrend; exit when price recovers to the middle band (the mean).

    Entry: close < SMA(n) - k*std(n), and (if `regime`) close > regime SMA.
    Exit:  close > SMA(n).
    """

    def __init__(self, n: int = 20, k: float = 2.0, regime: int | None = 200):
        self.n, self.k, self.regime = n, k, regime
        flt = f"_r{regime}" if regime else ""
        self.name = f"boll_{n}_{int(k * 10)}{flt}"

    def signals(self, prices: pd.DataFrame) -> pd.Series:
        c = prices["close"]
        mid = _sma(c, self.n)
        band = mid - self.k * c.rolling(self.n).std()
        trend_ok = (c > _sma(c, self.regime)) if self.regime else pd.Series(True, index=c.index)
        enter = ((c < band) & trend_ok).to_numpy()
        leave = (c > mid).to_numpy()
        sig = np.zeros(len(c), dtype=int)
        in_pos = False
        for t in range(len(c)):
            if not in_pos and enter[t]:
                in_pos = True
            elif in_pos and leave[t]:
                in_pos = False
            sig[t] = 1 if in_pos else 0
        return pd.Series(sig, index=c.index)


class HighProximity:
    """Momentum (George & Hwang): long while close sits within `within` of
    its prior `lookback`-day high — strength near 52-week highs persists."""

    def __init__(self, lookback: int = 252, within: float = 0.05):
        self.lookback, self.within = lookback, within
        self.name = f"high{lookback}_w{int(within * 100)}"

    def signals(self, prices: pd.DataFrame) -> pd.Series:
        c = prices["close"]
        hi = c.rolling(self.lookback).max().shift(1)
        return (c >= hi * (1 - self.within)).astype(int)


class VolTargetMomentum:
    """TSMOM with volatility-targeted sizing (fractional positions, 0..1).

    Weight = 1{trailing return > 0} * min(1, vol_target / realized vol).
    Never exceeds 1.0 — no leverage, cash-account/halal compatible. The
    institutions' standard Sharpe upgrade to plain trend following.

    RESEARCH-ONLY for now: the live signal Lambda sizes binary positions;
    promoting this needs signal_handler to size `allocation * weight`.
    """

    def __init__(self, lookback: int = 126, vol_target: float = 0.15,
                 vol_window: int = 21):
        self.lookback, self.vol_target = lookback, vol_target
        self.vol_window = vol_window
        self.name = f"tsmom_{lookback}_vt{int(vol_target * 100)}"

    def signals(self, prices: pd.DataFrame) -> pd.Series:
        c = prices["close"]
        trend = (c.pct_change(self.lookback) > 0).astype(float)
        realized = c.pct_change().rolling(self.vol_window).std() * np.sqrt(252)
        weight = (self.vol_target / realized).clip(upper=1.0).fillna(0.0)
        return trend * weight


class BuyAndHold:
    """Initially equal-weighted universe, then retain units without rebalancing."""
    name = "buy_and_hold"
    buy_and_hold = True

    def signals(self, prices: pd.DataFrame) -> pd.Series:
        return pd.Series(1, index=prices.index)


class CrossSectionalMomentum:
    """Rotation: hold the `top_n` strongest names in the universe by trailing
    `lookback`-day return.

    Every other strategy here judges a ticker in isolation. This one cannot:
    "strong" is only meaningful relative to peers, so it needs the whole
    watchlist at once. It therefore exposes `universe_signals` instead of
    `signals`, and `cross_sectional` tells the signal Lambda to feed it the
    full universe rather than one frame at a time.

    With `absolute_filter` it becomes dual momentum — relative strength AND
    a positive trailing return of its own, so it holds nothing at all when
    the whole universe is falling instead of rotating into the best loser.
    """

    cross_sectional = True

    def __init__(self, lookback: int = 126, top_n: int = 3,
                 absolute_filter: bool = False):
        self.lookback, self.top_n = lookback, top_n
        self.absolute_filter = absolute_filter
        kind = "dualmom" if absolute_filter else "xsmom"
        self.name = f"{kind}_{lookback}_top{top_n}"

    def scores(self, prices_by_symbol: dict[str, pd.DataFrame]) -> dict[str, float]:
        out = {}
        for sym, px in prices_by_symbol.items():
            c = px["close"]
            if len(c) <= self.lookback:
                continue
            r = c.pct_change(self.lookback).iloc[-1]
            if pd.notna(r):
                out[sym] = float(r)
        return out

    def universe_signals(self, prices_by_symbol: dict[str, pd.DataFrame]
                         ) -> dict[str, int]:
        sc = self.scores(prices_by_symbol)
        held = sorted(sc, key=lambda s: (-sc[s], s))[:self.top_n]
        if self.absolute_filter:
            held = [s for s in held if sc[s] > 0]
        return {sym: int(sym in held) for sym in prices_by_symbol}


class Ensemble:
    """Diversification across logic instead of across assets: long when at
    least `min_votes` of the member strategies agree.

    The members are deliberately different personalities — trend following
    holds through noise and wins rarely but big; mean reversion is in cash
    most of the time and wins often but small. Their bad stretches do not
    line up, so requiring agreement filters weak setups that any single
    member would have taken alone.
    """

    def __init__(self, members: list | None = None, min_votes: int = 2):
        self.members = list(members) if members else [
            TimeSeriesMomentum(126), RSIMeanReversion(), DonchianBreakout()]
        self.min_votes = min_votes
        self.name = f"ens_{min_votes}of{len(self.members)}"

    def signals(self, prices: pd.DataFrame) -> pd.Series:
        votes = sum(m.signals(prices) for m in self.members)
        return (votes >= self.min_votes).astype(int)


# ------------------------------------------------- cross-sectional ML ranker
# Version 2 of the ML books. The first model asked "will this stock rise over
# the next 5 days?". Stocks rise slightly more often than they fall, so it
# learned to say yes to almost everything and behaved like buy-and-hold.
# These ask "will this stock beat the other watchlist stocks over the next
# 5 days?" and hold the few ranked highest. The features add each stock's
# standing against its peers and the state of the whole watchlist.
# Shared by ml_rank.py (training) and MLRanker (inference); order matters,
# because exported models index their inputs by position.
PANEL_FEATURES = FEATURES + [
    "xs_r21", "xs_r126", "xs_vol21",        # percentile rank among peers that day
    "rel_r5",                               # 5-day return minus the peer median
    "mkt_r21", "mkt_vol21", "mkt_breadth",  # the watchlist's trend, volatility, breadth
]


def compute_panel_features(prices_by_symbol: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """PANEL_FEATURES for every (date, symbol) with a complete feature row.

    Strictly backward-looking: each row uses closes up to its own date only.
    A stock counts among the peers only on dates where its own features are
    complete, so a recent listing joins once it has enough history.
    """
    parts = []
    for symbol, prices in prices_by_symbol.items():
        own = compute_features(prices)
        own["symbol"] = symbol
        parts.append(own)
    panel = pd.concat(parts)
    panel.index.name = "date"
    panel = panel.reset_index().dropna(subset=FEATURES)
    panel = panel[np.isfinite(panel[FEATURES]).all(axis=1)]
    by_date = panel.groupby("date")
    panel["xs_r21"] = by_date["r21"].rank(pct=True)
    panel["xs_r126"] = by_date["r126"].rank(pct=True)
    panel["xs_vol21"] = by_date["vol21"].rank(pct=True)
    panel["rel_r5"] = panel["r5"] - by_date["r5"].transform("median")
    panel["mkt_r21"] = by_date["r21"].transform("median")
    panel["mkt_vol21"] = by_date["vol21"].transform("median")
    panel["mkt_breadth"] = (panel["dist_sma200"] > 0).astype(float).groupby(
        panel["date"]).transform("mean")
    return panel.set_index(["date", "symbol"]).sort_index()[PANEL_FEATURES]


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


class LogisticModel:
    """Standardized dot product; parameters exported by ml_rank.py."""

    def __init__(self, m: dict):
        self.mu = np.array(m["mu"], dtype=float)
        sigma = np.array(m["sigma"], dtype=float)
        self.sigma = np.where(sigma == 0, 1.0, sigma)
        self.coef = np.array(m["coef"], dtype=float)
        self.intercept = float(m["intercept"])

    def predict(self, X: np.ndarray) -> np.ndarray:
        return _sigmoid(((X - self.mu) / self.sigma) @ self.coef + self.intercept)


class TreeEnsembleModel:
    """Gradient-boosted trees, trained with LightGBM and exported as node arrays.

    Pure numpy, so the Lambda needs no ML library. Each tree is evaluated for
    every row at once: rows step one level down per pass (value <= threshold
    goes left) until all of them reach a leaf. The leaf values of all trees
    add up to a log-odds score, exactly as in LightGBM's binary objective.
    """

    def __init__(self, m: dict):
        self.trees = [{k: np.asarray(tree[k]) for k in ("feature", "threshold", "left", "right",
                                                        "value")} for tree in m["trees"]]

    def predict(self, X: np.ndarray) -> np.ndarray:
        raw = np.zeros(len(X))
        rows = np.arange(len(X))
        for tree in self.trees:
            node = np.zeros(len(X), dtype=int)
            while True:
                feature = tree["feature"][node]
                inner = feature >= 0  # leaves are marked with feature -1
                if not inner.any():
                    break
                at = node[inner]
                go_left = X[rows[inner], feature[inner]] <= tree["threshold"][at]
                node[inner] = np.where(go_left, tree["left"][at], tree["right"][at])
            raw += tree["value"][node]
        return _sigmoid(raw)


MODEL_TYPES = {"logistic_regression": LogisticModel, "gradient_boosted_trees": TreeEnsembleModel}


class MLRanker:
    """Hold the `top_n` stocks a model ranks most likely to beat their peers.

    Ranking uses each stock's average predicted probability over the last
    `smooth` sessions, which keeps one noisy day from replacing a holding.
    Only stocks with a prediction on the latest session are eligible. Always
    fully invested in its picks: it tests stock selection, not market timing.
    """

    cross_sectional = True

    def __init__(self, model_file: str, name: str, top_n: int = 5, smooth: int = 5,
                 model_dir: str | None = None):
        directory = model_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
        with open(os.path.join(directory, model_file)) as fh:
            m = json.load(fh)
        if m.get("features") != PANEL_FEATURES:
            raise ValueError(f"{model_file}: features don't match strategies.PANEL_FEATURES; retrain")
        if m.get("type") not in MODEL_TYPES:
            raise ValueError(f"{model_file}: unknown model type {m.get('type')!r}")
        self.model = MODEL_TYPES[m["type"]](m)
        self.meta, self.name = m, name
        self.top_n, self.smooth = top_n, smooth

    def probabilities(self, prices_by_symbol: dict[str, pd.DataFrame]) -> pd.Series:
        panel = compute_panel_features(prices_by_symbol)
        return pd.Series(self.model.predict(panel.to_numpy(dtype=float)), index=panel.index)

    def scores(self, prices_by_symbol: dict[str, pd.DataFrame]) -> pd.Series:
        p = self.probabilities(prices_by_symbol)
        if p.empty:
            return p
        dates = p.index.get_level_values("date")
        recent = sorted(dates.unique())[-self.smooth:]
        eligible = set(p[dates == recent[-1]].index.get_level_values("symbol"))
        mean = p[dates.isin(recent)].groupby(level="symbol").mean()
        return mean[mean.index.isin(eligible)]

    def universe_signals(self, prices_by_symbol: dict[str, pd.DataFrame]) -> dict[str, int]:
        ranked = sorted(self.scores(prices_by_symbol).items(), key=lambda kv: (-kv[1], kv[0]))
        held = {symbol for symbol, _ in ranked[:self.top_n]}
        return {symbol: int(symbol in held) for symbol in prices_by_symbol}


class BufferedRotation:
    """Rotation with a buffer: buy into the top `top_n` by trailing return,
    and keep a holding until it falls below rank `keep_until`.

    Plain top-3 rotation swapped names whenever 3rd and 4th place traded
    spots, which in the 2012-2026 backtest on the 50-stock list meant 48x
    annual turnover. Entering at 5 but exiting only below 10 cut that to 9x
    with about the same return, a smaller worst drawdown (-36% vs -46%) and
    a positive worst year (+1% vs -18%).

    Unlike every other strategy here this one depends on what it held
    yesterday, so it is `stateful`: callers pass the previous session's
    holdings, and without them it starts fresh from the current top_n.
    """

    cross_sectional = True
    stateful = True

    def __init__(self, lookback: int = 126, top_n: int = 5, keep_until: int = 10):
        self.lookback, self.top_n, self.keep_until = lookback, top_n, keep_until
        self.name = f"xsmom_{lookback}_top{top_n}_keep{keep_until}"

    def universe_signals(self, prices_by_symbol: dict[str, pd.DataFrame],
                         previous: set[str] | None = None) -> dict[str, int]:
        scores = CrossSectionalMomentum(self.lookback).scores(prices_by_symbol)
        order = sorted(scores, key=lambda s: (-scores[s], s))
        rank = {s: i + 1 for i, s in enumerate(order)}
        kept = sorted((s for s in (previous or set()) if rank.get(s, len(rank) + 1) <= self.keep_until),
                      key=rank.get)
        held = set(kept[:self.top_n])  # never more holdings than slots, best-ranked first
        for s in order:
            if len(held) >= self.top_n:
                break
            held.add(s)
        return {s: int(s in held) for s in prices_by_symbol}


class BreadthFilter:
    """Hold every stock equally while more than `threshold` of them trade
    above their `window`-day average; otherwise hold cash.

    The only book that times the whole market instead of picking stocks.
    In the 2012-2026 backtest it had the best risk-adjusted return of the
    books tested (Sharpe 1.22) and halved holding everything's worst
    drawdown (-18% vs -36%). Its weakness is whipsaw: a signal flipping
    near the threshold can sell low and buy back higher (worst year -16%).
    """

    cross_sectional = True
    top_n = 10**9  # one equal slot per stock in the universe

    def __init__(self, window: int = 200, threshold: float = 0.5):
        self.window, self.threshold = window, threshold
        self.name = f"breadth_all_sma{window}_gt{int(threshold * 100)}"

    def breadth(self, prices_by_symbol: dict[str, pd.DataFrame]) -> float:
        above = [bool(px["close"].iloc[-1] > px["close"].iloc[-self.window:].mean())
                 for px in prices_by_symbol.values() if len(px) >= self.window]
        return sum(above) / len(above) if above else 0.0

    def universe_signals(self, prices_by_symbol: dict[str, pd.DataFrame]) -> dict[str, int]:
        on = int(self.breadth(prices_by_symbol) > self.threshold)
        return {s: on for s in prices_by_symbol}


REGISTRY = {
    "buyhold": BuyAndHold,
    "dualmom": lambda: CrossSectionalMomentum(absolute_filter=True),
    "sma": SMACrossover,
    "tsmom": TimeSeriesMomentum,
    "dip": DipBuyer,
    "rsi": RSIMeanReversion,
    "donchian": DonchianBreakout,
    "macd": MACDCross,
    "boll": BollingerReversion,
    "high52": HighProximity,
    "voltarget": VolTargetMomentum,
    "ml": MLStrategy,
    "xsmom": CrossSectionalMomentum,
    "xsmom_buffered": BufferedRotation,
    "breadth": BreadthFilter,
    "ensemble": Ensemble,
}
