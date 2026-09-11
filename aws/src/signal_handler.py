"""Publish one complete daily signal batch, using only the prior session."""
import datetime as dt
import json
from pathlib import Path

import pandas as pd

from shared import experiment as exp, portfolio, runtime

MIN_BARS = 210


def lambda_handler(event, context):
    date = exp.today()
    alpaca, table = runtime.get_alpaca(), runtime.get_table()
    start = (dt.date.fromisoformat(date) - dt.timedelta(days=15)).isoformat()
    sessions = alpaca.calendar(start, date)
    if not any(s["date"] == date for s in sessions):
        return {"skipped": "not a trading session"}
    existing = exp.get(table, "RUN", date)
    if existing:
        return {"run_id": existing["run_id"], "reused": True, "targets": existing["targets"]}
    previous_date = max(s["date"] for s in sessions if s["date"] < date)
    symbols = sorted(set(runtime.get_watchlist()))
    if not symbols:
        raise ValueError("empty watchlist")
    frames = {}
    for symbol in symbols:
        bars = alpaca.daily_bars(symbol)
        usable = [b for b in bars if str(b["t"])[:10] <= previous_date]
        if len(usable) < MIN_BARS or str(usable[-1]["t"])[:10] != previous_date:
            raise ValueError(f"{symbol}: incomplete history for {previous_date}; no batch published")
        frame = pd.DataFrame({"close": [float(b["c"]) for b in usable]},
                             index=pd.to_datetime([str(b["t"])[:10] for b in usable]))
        if not frame.index.is_unique or not frame.index.is_monotonic_increasing or not frame.close.map(portfolio.positive).all():
            raise ValueError(f"{symbol}: invalid bars")
        frames[symbol] = frame
    lineup = runtime.get_lineup()
    budget = float(runtime.env("BOOK_BUDGET_USD", "12000"))
    cost = float(runtime.env("COST_BPS", "5"))
    if not portfolio.positive(budget) or not 0 <= cost < 10000:
        raise ValueError("invalid budget or cost")
    model = Path(__file__).parent / "models" / "logreg.json"
    config = dict(universe=symbols, budget=budget, cost_bps=cost,
                  strategies=[s.name for s in lineup], model_hash=exp.digest(model.read_text()),
                  accounting_version=2)
    saved_config = exp.put_once(table, "CONFIG", "settings", config)
    if saved_config != config:
        raise ValueError("Experiment settings changed. Use a new EXPERIMENT_ID.")
    targets, books = [], {}
    for strategy in lineup:
        weights = portfolio.weights_for(strategy, frames)
        books[strategy.name] = dict(weights=weights, buy_and_hold=getattr(strategy, "buy_and_hold", False))
        for symbol, weight in weights.items():
            targets.append(dict(strategy=strategy.name, symbol=symbol, signal=int(weight > 0),
                                weight=weight, price=float(frames[symbol].close.iloc[-1])))
    payload = dict(date=date, signal_date=previous_date, config=config, books=books, targets=targets,
                   ts=dt.datetime.now(dt.timezone.utc).isoformat())
    payload["run_id"] = exp.digest({k: v for k, v in payload.items() if k != "ts"})[:24]
    saved = exp.put_once(table, "RUN", date, payload)
    print(json.dumps({"date": date, "run_id": saved["run_id"], "targets": len(saved["targets"])}))
    return {"run_id": saved["run_id"], "targets": saved["targets"]}
