"""DynamoDB state for broker execution. Item shapes:

  pk=BOTPOS#{symbol}     sk=latest  shares the broker confirms the bot holds
  pk=INTENT#{client id}  sk=latest  written just before an order is submitted,
                                    so a lost HTTP response can be recovered
  pk=ORDER#{broker id}   sk=latest  latest observation of one broker order
  pk=TRADE#{symbol}      sk={ts}    legacy order log: read, no longer written
  pk=SNAP#{strategy}     sk={date}  legacy snapshots from the first experiment

The daily experiment records (RUN, LEDGER, PLAN, CONFIG) live in
experiment.py. Scans are fine here: the table holds hundreds of items.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

TERMINAL_ORDER_STATUSES = {"filled", "canceled", "expired", "rejected", "replaced"}


def _scan_all(table) -> list[dict]:
    items, kwargs = [], {"ConsistentRead": True}
    while True:
        resp = table.scan(**kwargs)
        items += resp.get("Items", [])
        last = resp.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


# ------------------------------------------------------------- positions
def put_bot_position(table, symbol: str, shares: int, ts: str) -> None:
    table.put_item(Item={
        "pk": f"BOTPOS#{symbol}", "sk": "latest",
        "symbol": symbol, "shares": int(shares), "ts": ts,
    })


def delete_bot_position(table, symbol: str) -> None:
    table.delete_item(Key={"pk": f"BOTPOS#{symbol}", "sk": "latest"})


def get_bot_symbols(table) -> list[str]:
    """Symbols the bot holds or has an order intent for."""
    return [i["symbol"] for i in _scan_all(table)
            if str(i.get("pk", "")).startswith(("BOTPOS#", "INTENT#"))]


# ---------------------------------------------------------------- orders
def put_intent(table, client_id, symbol):
    table.put_item(Item={"pk": f"INTENT#{client_id}", "sk": "latest", "symbol": symbol})


def unresolved_intents(table):
    return [i for i in _scan_all(table)
            if str(i.get("pk", "")).startswith("INTENT#") and not i.get("reconciled")]


def record_trade(table, symbol: str, side: str, qty: int,
                 order: dict, ts: str, price: float = 0.0) -> None:
    """Store the latest observation of a broker order, keyed by its stable ID."""
    table.put_item(Item={
        "pk": f"ORDER#{order['id']}", "sk": "latest",
        "submitted_at": str(order.get("submitted_at") or ts),
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "symbol": symbol, "side": side, "qty": int(qty),
        "price": Decimal(str(round(price, 4))),
        "order_id": str(order.get("id", "")),
        "status": str(order.get("status", "")),
        "client_order_id": str(order.get("client_order_id", "")),
        "filled_qty": Decimal(str(order.get("filled_qty") or 0)),
        "filled_avg_price": Decimal(str(order.get("filled_avg_price") or 0)),
    })


def _trade_records(table):
    """One observation per broker order, including older timestamp-keyed rows.

    Alpaca can revise submitted_at after acceptance, so the broker order ID,
    not that timestamp, is the stable identity. Legacy rows are kept but not
    published as additional fills.
    """
    def priority(item):
        return (item["sk"] == "latest", float(item.get("filled_qty", 0)),
                str(item.get("status", "")) in TERMINAL_ORDER_STATUSES,
                str(item.get("updated_at", item["sk"])))

    records = {}
    for item in _scan_all(table):
        if not str(item.get("pk", "")).startswith(("TRADE#", "ORDER#")):
            continue
        if item.get("order_id"):
            identity = str(item["order_id"])
        elif str(item["pk"]).startswith("ORDER#"):
            identity = str(item["pk"])
        else:  # a legacy TRADE# row with no order ID is its own record
            identity = str((item["pk"], item["sk"]))
        previous = records.get(identity)
        if previous is None or priority(item) > priority(previous):
            records[identity] = item
    return [dict(i, sk=str(i.get("submitted_at", i["sk"]))) for i in records.values()]


def get_trades(table, limit: int = 50) -> list[dict]:
    trades = _trade_records(table)
    return sorted(trades, key=lambda i: str(i["sk"]), reverse=True)[:limit]


def pending_trades(table):
    return [i for i in _trade_records(table)
            if str(i.get("status", "")) not in TERMINAL_ORDER_STATUSES and i.get("order_id")]


def refresh_orders(alpaca, table):
    """Refresh confirmed order state without submitting or canceling orders.

    Callers hold the executor lease, so a stale response cannot overwrite a
    newer observation from a concurrent executor or reporter invocation.
    """
    for intent in unresolved_intents(table):
        order = alpaca.order_by_client_id(str(intent["pk"]).split("#", 1)[1])
        if order:
            record_trade(table, order["symbol"], order["side"], int(order["qty"]),
                         order, str(order["submitted_at"]))
            table.put_item(Item=dict(intent, reconciled=True))
    for item in pending_trades(table):
        order = alpaca.order(str(item["order_id"]))
        record_trade(table, str(item["symbol"]), str(item["side"]), int(item["qty"]),
                     order, str(item["sk"]), float(item.get("price", 0)))


# --------------------------------------------------------- legacy snapshots
def put_snap(table, strategy: str, date: str, positions: dict[str, int],
             prices: dict[str, float], pnl_day: float, cum_pnl: float,
             cum_ret: float = 0.0) -> None:
    """A first-experiment snapshot row. Only tests write these now, to check
    that the current experiment leaves such legacy rows untouched."""
    table.put_item(Item={
        "pk": f"SNAP#{strategy}", "sk": date, "strategy": strategy,
        "positions": {s: int(q) for s, q in positions.items()},
        "prices": {s: Decimal(str(round(p, 4))) for s, p in prices.items()},
        "pnl_day": Decimal(str(round(pnl_day, 2))),
        "cum_pnl": Decimal(str(round(cum_pnl, 2))),
        "cum_ret": Decimal(str(round(cum_ret, 8))),
    })


def get_snaps(table, strategy: str) -> list[dict]:
    snaps = [i for i in _scan_all(table) if str(i.get("pk", "")) == f"SNAP#{strategy}"]
    return sorted(snaps, key=lambda i: str(i["sk"]))
