"""Paper execution of a complete daily signal batch.

This is separate from the shadow ledger: the ledger models fractional units
at the close, while this places whole-share market orders at about 15:45 New
York time. Checks, in the order they run:

  * paper endpoint only, and one invocation at a time (DynamoDB lease)
  * market open, a fresh complete RUN, and the watchlist it was built from
  * no open orders awaiting reconciliation, an active unblocked account,
    and no holdings the bot did not place (the account must be dedicated)
  * per symbol: a tradable asset with a recent price, otherwise that symbol
    waits for the next run while the others trade
  * per order: under MAX_ORDER_USD, and buys within cash not yet reserved

A saved plan and deterministic client order IDs make a retry reuse earlier
orders instead of duplicating them, including after an HTTP response is lost.
Only confirmed broker positions update BOTPOS.

Lots are fractional for every asset that allows fractional orders, so a
$12,000 book can hold a few hundred dollars of a $900 stock. An asset that
does not allow them is held in whole shares, and any fraction left there by
a corporate action (a stock dividend paid in shares, as SCCO did in November
2026) is kept and reported. Adjustments under MIN_ORDER_USD are skipped.
Remainders under one share never count as unmanaged holdings.
"""
import datetime as dt
import json
import math
from decimal import ROUND_DOWN, Decimal

from shared import experiment as exp, portfolio, runtime, state

PAPER_ENDPOINT = "https://paper-api.alpaca.markets"

# Assumed fill premium for sizing and order caps, so a buy still fits in cash
# if the price rises before the market order fills. An estimate, not a limit.
PRICE_BUFFER = 1.02

# A book keeps its share count in a name until the position's value drifts
# more than this fraction from the book's dollar target for it. Re-sizing
# from scratch every day turned ordinary price moves into 1-share buys and
# sells whenever whole-share rounding flipped across a boundary.
REBALANCE_BAND = 0.20

# A last trade older than this is not a usable execution price.
MAX_PRICE_AGE_S = 900

# Fractional lots are sized to this many decimal places (Alpaca accepts up to 9).
QTY_STEP = Decimal("0.0001")

# Adjustments worth less than this are skipped as noise; closing a whole
# position is always sent, whatever its size.
MIN_ORDER_USD = 5.0


def lambda_handler(event, context):
    table = runtime.get_table()
    remaining_s = context.get_remaining_time_in_millis() / 1000 if context else 180
    # The lease outlives this invocation, so a retry cannot overlap a run
    # that timed out but has not actually stopped.
    owner = exp.acquire_executor(table, duration=max(300, math.ceil(remaining_s) + 60))
    if owner is None:
        return _log({"skipped": "another executor invocation holds the lease"})
    try:
        return _log(_execute())
    finally:
        exp.release_executor(table, owner)


def _log(result):
    print(json.dumps(dict(event="execution_result", **result)))
    return result


def _qty(value):
    """A quantity for logs and JSON: an int for whole shares, else a float."""
    return int(value) if value == int(value) else float(value)


def reconcile(alpaca, table):
    """Refresh order states, then sync BOTPOS to the broker's real holdings.

    Returns each symbol's quantity exactly as the broker reports it, as a
    Decimal so a fractional remainder like 6.106 is not rounded. BOTPOS keeps
    the whole-share count, and is kept while any fraction remains so the bot
    still treats the name as its own and can sell the remainder.
    """
    state.refresh_orders(alpaca, table)
    positions = {p["symbol"]: Decimal(str(p["qty"])) for p in alpaca.positions()}
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for symbol in set(state.get_bot_symbols(table)):
        qty = positions.get(symbol, Decimal(0))
        if qty < 0:
            raise ValueError(f"{symbol}: unsupported short broker holding")
        if qty > 0:
            state.put_bot_position(table, symbol, qty, now)
        else:
            state.delete_bot_position(table, symbol)
    return positions


def _execute():
    alpaca, table = runtime.get_alpaca(), runtime.get_table()
    date = exp.today()
    dry_run = str(runtime.env("DRY_RUN", "true")).lower() != "false"

    def skip(reason):
        return {"skipped": reason, "dry_run": dry_run}

    if getattr(alpaca, "trading_base", PAPER_ENDPOINT) != PAPER_ENDPOINT:
        raise ValueError("executor is paper-only")
    held = reconcile(alpaca, table)
    if not alpaca.clock().get("is_open", False):
        return skip("market closed")

    run = exp.get(table, "RUN", date)
    if not run:
        return skip("no complete batch")
    age_min = (dt.datetime.now(dt.timezone.utc)
               - dt.datetime.fromisoformat(run["ts"])).total_seconds() / 60
    if not 0 <= age_min <= float(runtime.env("MAX_SIGNAL_AGE_MIN", "360")):
        return skip("stale batch")
    if sorted(set(runtime.get_watchlist())) != run["config"]["universe"]:
        return skip("watchlist changed since batch")
    if alpaca.open_orders():
        return skip("open orders awaiting reconciliation")

    account = alpaca.account()
    if (account.get("status") != "ACTIVE" or account.get("trading_blocked")
            or account.get("account_blocked") or account.get("trade_suspended_by_user")):
        return skip("account not available for trading")
    managed = set(state.get_bot_symbols(table))
    # A sub-share remainder is not someone else's position; ignoring it is safe
    # because the bot only ever trades a remainder in a name it manages.
    if any(symbol not in managed and (qty < 0 or qty >= 1) for symbol, qty in held.items()):
        return skip("unmanaged broker positions; dedicated account required")

    # Spend cash only, never margin buying power.
    cash = min(float(account["cash"]),
               float(account.get("non_marginable_buying_power", account["cash"])))
    if not math.isfinite(cash) or cash < 0:
        raise ValueError("invalid available cash")
    max_order = float(runtime.env("MAX_ORDER_USD", "15000"))
    if not portfolio.positive(max_order):
        raise ValueError("invalid order cap")

    symbols = sorted(set(run["config"]["universe"]) | managed)
    trades = alpaca.latest_trades(symbols)  # one request for every symbol
    prices, waiting, fractionable = {}, {}, {}
    for symbol in symbols:
        if held.get(symbol, 0) < 0:
            raise ValueError(f"{symbol}: unsupported short broker holding")
        asset = alpaca.asset(symbol)
        fractionable[symbol] = bool(asset.get("fractionable"))
        price, problem = _execution_price(asset, trades.get(symbol))
        if problem:
            waiting[symbol] = problem
        else:
            prices[symbol] = price
    if not prices:
        return skip("no symbol can trade today: "
                    + "; ".join(f"{s} {why}" for s, why in waiting.items()))

    kind = "PREVIEW" if dry_run else "PLAN"
    plan = exp.get(table, kind, date)
    if plan and plan["run_id"] != run["run_id"]:
        raise ValueError("saved plan does not match today's signal batch")
    if not plan:
        previous = _previous_plan(table, kind, date)
        fractional = {symbol for symbol, ok in fractionable.items() if ok}
        lots = size_lots(run, prices, previous["lots"] if previous else {}, fractional)
        desired = {symbol: 0.0 for symbol in symbols}
        for book_lots in lots.values():
            for symbol, lot in book_lots.items():
                desired[symbol] += lot["shares"]
        plan = exp.put_once(table, kind, date, dict(
            date=date, run_id=run["run_id"], desired=desired, lots=lots))

    actions = [{"symbol": s, "waiting": why} for s, why in waiting.items()]
    orders = []  # (symbol, side, qty)
    for symbol, qty in plan["desired"].items():
        if symbol not in prices:
            continue
        current = held.get(symbol, Decimal(0))
        target = Decimal(str(qty))
        if fractionable[symbol]:
            # Closing a position sells exactly what is held, fraction included.
            delta = -current if target == 0 else (target - current).quantize(QTY_STEP)
        else:
            delta = Decimal(int(target) - int(current))
            remainder = current - int(current)
            if remainder:
                actions.append({"symbol": symbol, "waiting": f"fractional remainder "
                                f"{_qty(remainder)} kept: asset does not allow fractional orders"})
        if not delta:
            continue
        if target != 0 and float(abs(delta)) * prices[symbol] < MIN_ORDER_USD:
            continue  # a few dollars of drift is not worth an order
        orders.append((symbol, "buy" if delta > 0 else "sell", abs(delta)))

    # Sells go first. Their proceeds are not spent in this invocation: the
    # broker reports that cash on a later run, so buys draw only on `cash`.
    for symbol, side, qty in sorted(orders, key=lambda order: (order[1] == "buy", order[0])):
        notional = float(qty) * prices[symbol] * PRICE_BUFFER
        client_id = "bot-" + exp.digest(
            [exp.experiment_id(), plan["run_id"], symbol, side])[:36]
        order = None if dry_run else alpaca.order_by_client_id(client_id)
        if order:  # placed by an earlier invocation today: record it, never resubmit
            state.record_trade(table, symbol, side, Decimal(str(order["qty"])), order,
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
            actions.append({"symbol": symbol, "side": side, "qty": _qty(qty), "dry_run": True})
            continue
        state.put_intent(table, client_id, symbol)
        # If this raises after the broker accepted the order, the next
        # invocation finds it by the same client ID instead of placing another.
        order = alpaca.submit_order(symbol, qty, side, client_order_id=client_id)
        state.record_trade(table, symbol, side, qty, order,
                           str(order.get("submitted_at") or run["ts"]), prices[symbol])
        actions.append({"symbol": symbol, "side": side, "qty": _qty(qty),
                        "status": order.get("status")})
    if not dry_run:
        reconcile(alpaca, table)
    return {"dry_run": dry_run, "run_id": run["run_id"], "actions": actions}


def _execution_price(asset, trade):
    """(price, None) for a tradable symbol, or (None, reason) if it must wait."""
    if asset.get("status") != "active" or not asset.get("tradable"):
        return None, "asset unavailable for trading"
    if not trade:
        return None, "missing/stale execution price"
    traded_at = dt.datetime.fromisoformat(str(trade["t"]).replace("Z", "+00:00"))
    age_s = (dt.datetime.now(dt.timezone.utc) - traded_at).total_seconds()
    if not portfolio.positive(trade["p"]) or not -5 <= age_s <= MAX_PRICE_AGE_S:
        return None, "missing/stale execution price"
    return float(trade["p"]), None


def _previous_plan(table, kind, date):
    """The latest saved plan before `date` that recorded per-book lots.

    Plans saved before lots were introduced have neither a date nor lots and
    are ignored; the first plan after the change sizes every lot fresh.
    """
    earlier = [plan for plan in exp.history(table, kind)
               if "lots" in plan and plan.get("date", "") < date]
    return max(earlier, key=lambda plan: plan["date"]) if earlier else None


def size_lots(run, prices, previous_lots, fractional=frozenset()):
    """Shares per (book, symbol) for the broker plan.

    Each active book targets budget x weight dollars in every name it holds.
    Symbols in `fractional` are sized exactly, to QTY_STEP; the rest in whole
    shares, with the price buffer so a buy still fits in cash. A lot carried
    over from the previous plan keeps its share count while the book still
    holds the name at the same weight and the lot's value stays within
    REBALANCE_BAND of that target; otherwise it is re-sized at today's price.
    A symbol with no usable price keeps any lot it already had and is sized
    once it trades. Buy & hold is a shadow benchmark and has no lots.
    """
    budget = float(run["config"]["budget"])
    lots = {}
    for book, decision in run["books"].items():
        if decision["buy_and_hold"]:
            continue
        carried_lots = previous_lots.get(book, {})
        book_lots = {}
        for symbol, weight in decision["weights"].items():
            if weight <= 0:
                continue
            target = budget * weight
            carried = carried_lots.get(symbol)
            still_held = carried is not None and abs(carried["weight"] - weight) < 1e-9
            price = prices.get(symbol)
            if price is None:
                if still_held:
                    book_lots[symbol] = carried
                continue
            within_band = (still_held and carried["shares"] > 0
                           and abs(carried["shares"] * price - target) <= REBALANCE_BAND * target)
            if within_band:
                book_lots[symbol] = carried
            elif symbol in fractional:
                shares = Decimal(str(target / price)).quantize(QTY_STEP, rounding=ROUND_DOWN)
                book_lots[symbol] = {"weight": weight, "shares": float(shares)}
            else:
                book_lots[symbol] = {"weight": weight,
                                     "shares": int(target / (price * PRICE_BUFFER))}
        lots[book] = book_lots
    return lots
