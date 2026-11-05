"""Cash-inclusive shadow accounting, shared with the portfolio backtester.

Fractional synthetic units at adjusted closes; these are NOT broker fills.
Signals known before a session rebalance at that session's close. Existing
holdings earn the intervening move. All dollar amounts are full precision
internally; rounding is only for display.
"""
from __future__ import annotations

import math


def positive(value):
    return math.isfinite(float(value)) and float(value) > 0


def weights_for(strategy, frames):
    if not frames:
        raise ValueError("empty universe")
    if getattr(strategy, "cross_sectional", False):
        signals = strategy.universe_signals(frames)
        slots = min(strategy.top_n, len(frames))
    else:
        signals = {s: float(strategy.signals(px).iloc[-1]) for s, px in frames.items()}
        slots = len(frames)
    weights = {s: float(signals.get(s, 0)) / slots for s in frames}
    if any(not math.isfinite(w) or not 0 <= w <= 1 for w in weights.values()):
        raise ValueError("invalid long-only weight")
    if sum(weights.values()) > 1 + 1e-9:
        raise ValueError("weights exceed book budget")
    return weights


def advance(previous, prices, weights, budget=12000, cost_bps=5,
            hold=False, previous_prices=None):
    """Mark old holdings, sell reductions, then buy within available cash.

    previous_prices: revised adjusted closes for the prior snapshot date.
    Rescaling synthetic units preserves value across vendor adjustments.
    """
    if not positive(budget) or not 0 <= cost_bps < 10000:
        raise ValueError("invalid budget or cost")
    if any(not positive(p) for p in prices.values()):
        raise ValueError("missing/nonpositive/nonfinite close")
    if (any(not math.isfinite(w) or w < 0 for w in weights.values())
            or sum(weights.values()) > 1 + 1e-9):
        raise ValueError("invalid weights")
    positions = {s: float(q) for s, q in (previous or {}).get("positions", {}).items()}
    if not set(positions).union(weights) <= set(prices):
        raise ValueError("missing held or target price")
    if previous_prices is not None and previous:
        for s in positions:
            if not positive(previous_prices.get(s, 0)):
                raise ValueError("missing prior adjusted close")
            positions[s] *= float(previous["prices"][s]) / previous_prices[s]
    cash = float(previous["cash"]) if previous else float(budget)
    start_equity = float(previous["equity"]) if previous else float(budget)
    equity = cash + sum(q * prices[s] for s, q in positions.items())
    cost, rate = 0.0, cost_bps / 10000
    if not (hold and previous):
        desired = {s: equity * w / prices[s] for s, w in weights.items() if w > 0}
        for s, qty in list(positions.items()):
            sold = max(0, qty - desired.get(s, 0))
            value = sold * prices[s]
            cash += value * (1 - rate)
            cost += value * rate
            positions[s] -= sold
        purchases = {s: max(0, qty - positions.get(s, 0)) for s, qty in desired.items()}
        needed = sum(q * prices[s] * (1 + rate) for s, q in purchases.items())
        scale = min(1, max(0, cash) / needed) if needed else 0
        for s, qty in purchases.items():
            bought = qty * scale
            value = bought * prices[s]
            cash -= value * (1 + rate)
            cost += value * rate
            positions[s] = positions.get(s, 0) + bought
    if cash < -1e-6:
        raise ValueError("negative cash")
    cash = max(0, cash)
    positions = {s: q for s, q in positions.items() if q > 1e-10}
    capital = sum(q * prices[s] for s, q in positions.items())
    equity = cash + capital
    return dict(positions=positions, prices={s: prices[s] for s in positions},
                cash=cash, capital=capital, equity=equity, initial_budget=budget,
                day_pnl=equity-start_equity, cum_pnl=equity-budget,
                day_ret=equity/start_equity-1, cum_ret=equity/budget-1, costs=cost)
