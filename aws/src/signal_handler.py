"""Publish one complete daily signal batch, decided near the close.

Runs at 15:40 New York time on trading days. Each ticker's frame is its
completed daily bars through the previous session plus one more row: its
latest trade today, standing in for today's close 20 minutes early. The
executor then trades at 15:45 and the shadow books rebalance at the close,
so a signal acts on the same session's prices instead of a day later.
Nothing after 15:40 can reach a signal, so there is no lookahead.

Until November 23, 2026 the batch used only completed sessions. A 2010-2026
backtest on the watchlist found deciding near the close improved every
strategy tested, most of all the short-horizon RSI-2 (Sharpe 0.17 -> 0.26).

A ticker with no trade yet today (halted, or a data gap) falls back to its
completed bars and is listed under `prior_close_symbols`. The batch is
written once (RUN#<experiment>/<date>); a rerun returns it unchanged.
"""
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from shared import experiment as exp, portfolio, runtime

MIN_BARS = 210  # the 200-day trend filters, plus a buffer
NEW_YORK = ZoneInfo("America/New_York")


def lambda_handler(event, context):
    date = exp.today()
    alpaca, table = runtime.get_alpaca(), runtime.get_table()
    window_start = (dt.date.fromisoformat(date) - dt.timedelta(days=15)).isoformat()
    sessions = sorted(s["date"] for s in alpaca.calendar(window_start, date))
    if date not in sessions:
        return {"skipped": "not a trading session"}
    existing = exp.get(table, "RUN", date)
    if existing:
        return {"run_id": existing["run_id"], "reused": True, "targets": existing["targets"]}

    earlier = [s for s in sessions if s < date]
    previous_date = earlier[-1]
    # A symbol whose bar for previous_date has not arrived may use the session
    # before it rather than cancel the whole day; anything older still fails.
    fallback_date = earlier[-2] if len(earlier) > 1 else None

    symbols = sorted(set(runtime.get_watchlist()))
    if not symbols:
        raise ValueError("empty watchlist")
    frames, late, prior_close = {}, [], []
    for symbol in symbols:
        frame, through = _frame(alpaca.daily_bars(symbol), symbol, previous_date)
        if through != previous_date:
            if through != fallback_date:
                raise ValueError(f"{symbol}: incomplete history for {previous_date}; "
                                 "no batch published")
            late.append(symbol)
        price = _price_today(alpaca, symbol, date)
        if price is None:
            prior_close.append(symbol)
        else:
            today = pd.DataFrame({"close": [price]}, index=pd.to_datetime([date]))
            frame = pd.concat([frame, today])
        frames[symbol] = frame

    lineup = runtime.get_lineup()
    budget = float(runtime.env("BOOK_BUDGET_USD", "12000"))
    cost_bps = float(runtime.env("COST_BPS", "5"))
    if not portfolio.positive(budget) or not 0 <= cost_bps < 10000:
        raise ValueError("invalid budget or cost")

    # The settings that define the experiment, frozen on its first run.
    # Changing any of them needs a new EXPERIMENT_ID so results stay comparable.
    model = Path(__file__).parent / "models" / "logreg.json"
    config = dict(universe=symbols, budget=budget, cost_bps=cost_bps,
                  strategies=[s.name for s in lineup],
                  model_hash=exp.digest(model.read_text()), accounting_version=2)
    if exp.put_once(table, "CONFIG", "settings", config) != config:
        raise ValueError("Experiment settings changed. Use a new EXPERIMENT_ID.")

    targets, books = [], {}
    for strategy in lineup:
        weights = portfolio.weights_for(strategy, frames)
        books[strategy.name] = dict(weights=weights,
                                    buy_and_hold=getattr(strategy, "buy_and_hold", False))
        for symbol, weight in weights.items():
            targets.append(dict(strategy=strategy.name, symbol=symbol, signal=int(weight > 0),
                                weight=weight, price=float(frames[symbol]["close"].iloc[-1])))

    # signal_date stays the last completed session; signal_timing says whether
    # today's prices were also used, so every batch states what it was built on.
    payload = dict(date=date, signal_date=previous_date, signal_timing="near_close",
                   config=config, books=books, targets=targets,
                   ts=dt.datetime.now(dt.timezone.utc).isoformat())
    if late:
        payload["late_symbols"] = late
    if prior_close:
        payload["prior_close_symbols"] = prior_close
    payload["run_id"] = exp.digest({k: v for k, v in payload.items() if k != "ts"})[:24]
    saved = exp.put_once(table, "RUN", date, payload)
    print(json.dumps({"date": date, "run_id": saved["run_id"], "targets": len(saved["targets"]),
                      "late_symbols": saved.get("late_symbols", []),
                      "prior_close_symbols": saved.get("prior_close_symbols", [])}))
    return {"run_id": saved["run_id"], "targets": saved["targets"]}


def _price_today(alpaca, symbol, date):
    """The latest trade from today's session in New York, or None without one.

    A trade from an earlier day is not today's price, so it is not used. On an
    early-close day the last trade is that session's close, which is correct.
    A failed request also returns None: the ticker then decides on its prior
    close and is listed in the batch, rather than the whole batch failing.
    """
    try:
        trade = alpaca.latest_trade(symbol)
        traded_at = dt.datetime.fromisoformat(str(trade["t"]).replace("Z", "+00:00"))
    except Exception as exc:
        print(json.dumps({"event": "no_price_today", "symbol": symbol,
                          "error_type": type(exc).__name__}))
        return None
    if traded_at.astimezone(NEW_YORK).date().isoformat() != date:
        return None
    if not portfolio.positive(trade["p"]):
        return None
    return float(trade["p"])


def _frame(bars, symbol, through):
    """Validated closes up to `through` (inclusive), and the date of the last one."""
    usable = [b for b in bars if str(b["t"])[:10] <= through]
    if len(usable) < MIN_BARS:
        raise ValueError(f"{symbol}: incomplete history for {through}; no batch published")
    frame = pd.DataFrame({"close": [float(b["c"]) for b in usable]},
                         index=pd.to_datetime([str(b["t"])[:10] for b in usable]))
    if (not frame.index.is_unique or not frame.index.is_monotonic_increasing
            or not frame["close"].map(portfolio.positive).all()):
        raise ValueError(f"{symbol}: invalid bars")
    return frame, str(usable[-1]["t"])[:10]
