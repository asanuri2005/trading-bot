"""Cash-inclusive shadow books. No broker fills are inferred from targets.

A new experiment namespace preserves old SNAP rows. One atomic daily ledger
contains all books; reruns republish it without changing recorded decisions.
"""
import datetime as dt
import json
from zoneinfo import ZoneInfo

from shared import experiment as exp, portfolio, runtime, state

DISPLAY = {"tsmom_126": "TSMOM 6mo", "rsi2_lt10_r200": "RSI-2 + 200d",
           "ml_logreg": "ML LogReg", "xsmom_126_top3": "Rotation top-3",
           "dualmom_126_top3": "Dual momentum", "ens_2of3": "Ensemble 2-of-3",
           "buy_and_hold": "Buy & hold"}


def lambda_handler(event, context):
    date = (event or {}).get("as_of_date", exp.today())
    alpaca, table = runtime.get_alpaca(), runtime.get_table()
    window_start = (dt.date.fromisoformat(date) - dt.timedelta(days=15)).isoformat()
    sessions = alpaca.calendar(window_start, date)
    session = next((s for s in sessions if s["date"] == date), None)
    if session is None:
        return {"skipped": "not a trading session"}
    closes_at = dt.datetime.fromisoformat(f"{date}T{session['close']}").replace(
        tzinfo=ZoneInfo("America/New_York"))
    if dt.datetime.now(dt.timezone.utc) < closes_at:
        return {"skipped": "session has not closed"}
    run = exp.get(table, "RUN", date)
    if not run:
        raise ValueError("No complete signal batch for this session; ledger unchanged")
    records = exp.history(table, "LEDGER")
    existing = next((r for r in records if r["date"] == date), None)
    if not existing:
        if any(r["date"] > date for r in records):
            raise ValueError("cannot insert into existing history")
        prev = records[-1] if records else None
        previous_session = max(s["date"] for s in sessions if s["date"] < date)
        if prev and prev["date"] != previous_session:
            raise ValueError("Missing session: replay stored RUN records in date order first")
        symbols = run["config"]["universe"]
        prices, previous_prices = {}, {}
        span = max(20, (dt.date.today()-dt.date.fromisoformat(date)).days + 25)
        for symbol in symbols:
            bars = alpaca.daily_bars(symbol, limit=span)
            daily = {str(b["t"])[:10]: float(b["c"]) for b in bars}
            prices[symbol] = daily.get(date, 0)
            if prev:
                previous_prices[symbol] = daily.get(prev["date"], 0)
        if any(not portfolio.positive(p) for p in prices.values()):
            raise ValueError("missing current-session close; ledger unchanged")
        books = {}
        for name, decision in run["books"].items():
            books[name] = portfolio.advance(
                prev["books"][name] if prev else None, prices, decision["weights"],
                budget=run["config"]["budget"], cost_bps=run["config"]["cost_bps"],
                hold=decision["buy_and_hold"], previous_prices=previous_prices if prev else None)
        exp.put_once(table, "LEDGER", date,
                     dict(date=date, books=books, run_id=run["run_id"], config=run["config"]))
        records = exp.history(table, "LEDGER")
    # Backfills never roll the public dashboard backwards.
    latest = records[-1]
    latest_run = exp.get(table, "RUN", latest["date"])
    reports = []
    for name, book in latest["books"].items():
        reports.append(dict(book, name=name, display=DISPLAY.get(name, name),
                            series=[dict(date=r["date"], cum_ret=r["books"][name]["cum_ret"],
                                         cum_pnl=r["books"][name]["cum_pnl"]) for r in records]))
    try:
        account = {"equity": float(alpaca.account()["equity"])}
    except Exception:
        account = {"equity": None, "unavailable": True}
    # Orders may finish after the 15:45 executor returns. Refresh their
    # confirmed fills before publishing, including recovered HTTP responses.
    # The shared lease protects order records; no orders are submitted here.
    orders_refreshed = False
    remaining = context.get_remaining_time_in_millis()/1000 if context else 180
    owner = exp.acquire_executor(table, duration=max(300, int(remaining)+61))
    if owner:
        try:
            state.refresh_orders(alpaca, table)
            orders_refreshed = True
        except Exception as exc:
            print(json.dumps({"event": "order_refresh_failed", "error_type": type(exc).__name__}))
        finally:
            exp.release_executor(table, owner)
    trades = [dict(ts=str(t["sk"]), symbol=str(t["symbol"]), side=str(t["side"]),
                   qty=float(t["qty"]), price=float(t.get("filled_avg_price", 0)),
                   filled_qty=float(t.get("filled_qty", 0)), status=str(t.get("status", "")))
              for t in state.get_trades(table)]
    data = dict(schema_version=2, experiment_id=exp.experiment_id(),
                start_date=records[0]["date"], as_of=latest["date"],
                generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                accounting="shadow_cash_inclusive", cost_bps=latest["config"]["cost_bps"],
                book_budget=latest["config"]["budget"], model_hash=latest["config"]["model_hash"],
                account=account, orders_refreshed=orders_refreshed,
                strategies=reports, targets=latest_run["targets"], trades=trades)
    bucket = runtime.env("DASHBOARD_BUCKET", "")
    if bucket:
        runtime.get_s3().put_object(Bucket=bucket, Key="data.json",
                                    Body=json.dumps(data, allow_nan=False),
                                    ContentType="application/json", CacheControl="no-store")
    return {"as_of": latest["date"], "strategies": len(reports), "published": bool(bucket)}
