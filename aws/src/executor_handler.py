"""Paper execution of complete batches; independent from the shadow ledger.

Pending orders block new submissions. Plans and client IDs survive retries.
Only confirmed broker positions update BOTPOS; submissions are order records.
"""
import datetime as dt
import json
import math

from shared import experiment as exp, portfolio, runtime, state


def reconcile(alpaca, table):
    state.refresh_orders(alpaca, table)
    positions = {p["symbol"]: float(p["qty"]) for p in alpaca.positions()}
    for symbol in set(state.get_bot_symbols(table)):
        qty = float(positions.get(symbol, 0))
        if qty < 0 or not qty.is_integer():
            raise ValueError(f"{symbol}: unsupported short/fractional broker holding")
        if qty:
            state.put_bot_position(table, symbol, int(qty), dt.datetime.now(dt.timezone.utc).isoformat())
        else:
            state.delete_bot_position(table, symbol)
    return positions


def lambda_handler(event, context):
    table = runtime.get_table()
    remaining = context.get_remaining_time_in_millis()/1000 if context else 180
    owner = exp.acquire_executor(table, duration=max(300, math.ceil(remaining)+60))
    if owner is None:
        result = {"skipped": "another executor invocation holds the lease"}
        print(json.dumps(dict(event="execution_result", **result)))
        return result
    try:
        result = _execute(event, context)
        print(json.dumps(dict(event="execution_result", **result)))
        return result
    finally:
        exp.release_executor(table, owner)


def _execute(event, context):
    alpaca, table = runtime.get_alpaca(), runtime.get_table()
    now = dt.datetime.now(dt.timezone.utc)
    date = exp.today()
    dry_run = str(runtime.env("DRY_RUN", "true")).lower() != "false"
    # Never permit a configured live-money endpoint here.
    if getattr(alpaca, "trading_base", "https://paper-api.alpaca.markets") != "https://paper-api.alpaca.markets":
        raise ValueError("executor is paper-only")
    held = reconcile(alpaca, table)
    if not alpaca.clock().get("is_open", False):
        return {"skipped": "market closed", "dry_run": dry_run}
    run = exp.get(table, "RUN", date)
    if not run:
        return {"skipped": "no complete batch", "dry_run": dry_run}
    age = (now-dt.datetime.fromisoformat(run["ts"])).total_seconds()/60
    if not 0 <= age <= float(runtime.env("MAX_SIGNAL_AGE_MIN", "360")):
        return {"skipped": "stale batch", "dry_run": dry_run}
    if sorted(set(runtime.get_watchlist())) != run["config"]["universe"]:
        return {"skipped": "watchlist changed since batch", "dry_run": dry_run}
    if alpaca.open_orders():
        return {"skipped": "open orders awaiting reconciliation", "dry_run": dry_run}
    account = alpaca.account()
    if (account.get("status") != "ACTIVE" or account.get("trading_blocked")
            or account.get("account_blocked") or account.get("trade_suspended_by_user")):
        return {"skipped": "account not available for trading", "dry_run": dry_run}
    managed = set(state.get_bot_symbols(table))
    if any(qty and symbol not in managed for symbol, qty in held.items()):
        return {"skipped": "unmanaged broker positions; dedicated account required", "dry_run": dry_run}
    cash = min(float(account["cash"]), float(account.get("non_marginable_buying_power", account["cash"])))
    if not math.isfinite(cash) or cash < 0:
        raise ValueError("invalid available cash")
    symbols = sorted(set(run["config"]["universe"]) | managed)
    prices = {}
    for symbol in symbols:
        asset = alpaca.asset(symbol)
        if asset.get("status") != "active" or not asset.get("tradable"):
            return {"skipped": f"asset unavailable for trading: {symbol}", "dry_run": dry_run}
        trade = alpaca.latest_trade(symbol)
        trade_age = (dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(str(trade["t"]).replace("Z", "+00:00"))).total_seconds()
        if not portfolio.positive(trade["p"]) or not -5 <= trade_age <= 900:
            return {"skipped": f"missing/stale execution price: {symbol}", "dry_run": dry_run}
        prices[symbol] = float(trade["p"])
        if held.get(symbol, 0) < 0 or not float(held.get(symbol, 0)).is_integer():
            raise ValueError(f"{symbol}: unsupported broker holding")
    kind = "PREVIEW" if dry_run else "PLAN"
    plan = exp.get(table, kind, date)
    if plan and plan["run_id"] != run["run_id"]:
        raise ValueError("saved plan does not match today's signal batch")
    if not plan:
        # B&H reference is monitored in the shadow ledger only. Broker orders
        # execute the six active strategies with equal fixed dollar budgets.
        desired = {s: 0 for s in symbols}
        for name, decision in run["books"].items():
            if decision["buy_and_hold"]:
                continue
            for symbol, weight in decision["weights"].items():
                desired[symbol] += int(run["config"]["budget"] * weight / (prices[symbol] * 1.02))
        plan = exp.put_once(table, kind, date, dict(desired=desired, run_id=run["run_id"]))
    actions = []
    max_order = float(runtime.env("MAX_ORDER_USD", "15000"))
    if not portfolio.positive(max_order):
        raise ValueError("invalid order cap")
    # Sells first; do not spend their proceeds until the broker reports cash
    # on a later invocation. Price buffer is an estimate, not a fill guarantee.
    changes = [(s, int(q)-int(held.get(s, 0))) for s, q in plan["desired"].items()]
    for symbol, delta in sorted(changes, key=lambda x: (x[1] > 0, x[0])):
        if not delta:
            continue
        side, qty = ("buy" if delta > 0 else "sell"), abs(delta)
        notional = qty * prices[symbol] * 1.02
        client_id = "bot-" + exp.digest([exp.experiment_id(), plan["run_id"], symbol, side])[:36]
        order = alpaca.order_by_client_id(client_id) if not dry_run else None
        if order:
            state.record_trade(table, symbol, side, int(order["qty"]), order,
                               str(order.get("submitted_at") or run["ts"]), prices[symbol])
            actions.append({"symbol": symbol, "status": order["status"], "reused": True})
            continue
        if notional > max_order:
            actions.append({"symbol": symbol, "blocked": "exceeds MAX_ORDER_USD"})
            continue
        if side == "buy" and notional > cash:
            actions.append({"symbol": symbol, "blocked": "insufficient unreserved cash"})
            continue
        if side == "buy":
            cash -= notional
        if dry_run:
            actions.append(dict(symbol=symbol, side=side, qty=qty, dry_run=True))
            continue
        state.put_intent(table, client_id, symbol)
        # On uncertain HTTP failure, propagate. The next invocation looks up
        # this same client ID instead of creating a different order.
        order = alpaca.submit_order(symbol, qty, side, client_order_id=client_id)
        state.record_trade(table, symbol, side, qty, order,
                           str(order.get("submitted_at") or run["ts"]), prices[symbol])
        actions.append(dict(symbol=symbol, side=side, qty=qty, status=order.get("status")))
    if not dry_run:
        reconcile(alpaca, table)
    return dict(dry_run=dry_run, run_id=run["run_id"], actions=actions)
