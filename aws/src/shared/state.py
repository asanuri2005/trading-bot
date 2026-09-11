"""DynamoDB single-table state. Item shapes:

  pk=TARGET#{strategy}#{symbol}  sk=latest   desired shares + signal snapshot
  pk=BOTPOS#{symbol}             sk=latest   shares the bot believes it holds
                                             (used to unwind positions whose
                                             ticker left the watchlist)
  pk=TRADE#{symbol}              sk={iso ts} submitted order log

Scans are fine here: the table holds tens of items, not millions.
"""
from __future__ import annotations

from decimal import Decimal


def _scan_all(table) -> list[dict]:
    items, kwargs = [], {}
    while True:
        resp = table.scan(**kwargs)
        items += resp.get("Items", [])
        last = resp.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


def put_target(table, strategy: str, symbol: str, signal: int,
               shares: int, price: float, ts: str) -> None:
    table.put_item(Item={
        "pk": f"TARGET#{strategy}#{symbol}", "sk": "latest",
        "strategy": strategy, "symbol": symbol, "signal": int(signal),
        "shares": int(shares), "price": Decimal(str(round(price, 4))),
        "ts": ts,
    })


def get_targets(table) -> list[dict]:
    return [i for i in _scan_all(table) if str(i.get("pk", "")).startswith("TARGET#")]


def put_bot_position(table, symbol: str, shares: int, ts: str) -> None:
    table.put_item(Item={
        "pk": f"BOTPOS#{symbol}", "sk": "latest",
        "symbol": symbol, "shares": int(shares), "ts": ts,
    })


def delete_bot_position(table, symbol: str) -> None:
    table.delete_item(Key={"pk": f"BOTPOS#{symbol}", "sk": "latest"})


def get_bot_symbols(table) -> list[str]:
    return [i["symbol"] for i in _scan_all(table)
            if str(i.get("pk", "")).startswith(("BOTPOS#", "INTENT#"))]


def record_trade(table, symbol: str, side: str, qty: int,
                 order: dict, ts: str, price: float = 0.0) -> None:
    table.put_item(Item={
        "pk": f"ORDER#{order['id']}", "sk": ts,
        "symbol": symbol, "side": side, "qty": int(qty),
        "price": Decimal(str(round(price, 4))),
        "order_id": str(order.get("id", "")),
        "status": str(order.get("status", "")),
        "client_order_id": str(order.get("client_order_id", "")),
        "filled_qty": Decimal(str(order.get("filled_qty") or 0)),
        "filled_avg_price": Decimal(str(order.get("filled_avg_price") or 0)),
    })


def get_trades(table, limit: int = 50) -> list[dict]:
    trades = [i for i in _scan_all(table)
              if str(i.get("pk", "")).startswith(("TRADE#", "ORDER#"))]
    return sorted(trades, key=lambda i: str(i["sk"]), reverse=True)[:limit]


def pending_trades(table):
    terminal = {"filled", "canceled", "expired", "rejected", "replaced"}
    return [i for i in _scan_all(table) if str(i.get("pk", "")).startswith(("ORDER#", "TRADE#"))
            and str(i.get("status", "")) not in terminal and i.get("order_id")]


def put_intent(table, client_id, symbol):
    table.put_item(Item={"pk": f"INTENT#{client_id}", "sk": "latest", "symbol": symbol})


def unresolved_intents(table):
    return [i for i in _scan_all(table) if str(i.get("pk", "")).startswith("INTENT#") and not i.get("reconciled")]


def put_snap(table, strategy: str, date: str, positions: dict[str, int],
             prices: dict[str, float], pnl_day: float, cum_pnl: float,
             cum_ret: float = 0.0) -> None:
    """One mark-to-market snapshot per strategy per trading day.

    `cum_ret` is the chained time-weighted return, stored alongside dollars
    because books deploy very different amounts of capital — a book holding
    three names cannot be compared to one holding twelve on dollars alone.
    """
    table.put_item(Item={
        "pk": f"SNAP#{strategy}", "sk": date, "strategy": strategy,
        "positions": {s: int(q) for s, q in positions.items()},
        "prices": {s: Decimal(str(round(p, 4))) for s, p in prices.items()},
        "pnl_day": Decimal(str(round(pnl_day, 2))),
        "cum_pnl": Decimal(str(round(cum_pnl, 2))),
        "cum_ret": Decimal(str(round(cum_ret, 8))),
    })


def get_snaps(table, strategy: str) -> list[dict]:
    snaps = [i for i in _scan_all(table)
             if str(i.get("pk", "")) == f"SNAP#{strategy}"]
    return sorted(snaps, key=lambda i: str(i["sk"]))


def get_latest_snap_before(table, strategy: str, date: str) -> dict | None:
    """Latest snapshot strictly before `date` — makes same-day reruns safe."""
    earlier = [s for s in get_snaps(table, strategy) if str(s["sk"]) < date]
    return earlier[-1] if earlier else None
