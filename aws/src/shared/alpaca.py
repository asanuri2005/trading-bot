"""Minimal Alpaca REST client (paper trading + market data), stdlib only.

Zero third-party HTTP deps means trivial Lambda packaging. Endpoints are
Alpaca's v2 APIs; verify against https://docs.alpaca.markets if they drift.
"""
from __future__ import annotations

import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request

TRADING_PAPER = "https://paper-api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"

RETRY_STATUSES = {429, 500, 502, 503, 504}
RETRY_DELAYS_S = (1, 3)  # waits before the second and third attempts of a read


class AlpacaError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Alpaca:
    def __init__(self, key_id: str, secret: str,
                 trading_base: str = TRADING_PAPER, data_base: str = DATA_BASE):
        self.key_id, self.secret = key_id, secret
        self.trading_base, self.data_base = trading_base, data_base

    # ------------------------------------------------------------ plumbing
    def _request(self, base: str, method: str, path: str,
                 params: dict | None = None, body: dict | None = None):
        url = base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url, method=method,
            headers={
                "APCA-API-KEY-ID": self.key_id,
                "APCA-API-SECRET-KEY": self.secret,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            data=json.dumps(body).encode() if body is not None else None,
        )
        # Reads are retried on throttling, server errors and network failures,
        # so one transient blip doesn't cancel a day's batch. Order submission
        # (POST) never is: a blind retry could place a second order, and the
        # executor already recovers lost responses by client order ID.
        attempts = len(RETRY_DELAYS_S) + 1 if method == "GET" else 1
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    payload = resp.read().decode()
                return json.loads(payload) if payload else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:300]
                if last or exc.code not in RETRY_STATUSES:
                    raise AlpacaError(f"{method} {path} -> HTTP {exc.code}: {detail}",
                                      exc.code) from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if last:
                    raise
            time.sleep(RETRY_DELAYS_S[attempt])

    # ---------------------------------------------------------- market data
    def daily_bars(self, symbol: str, limit: int = 320,
                   adjustment: str = "all", feed: str = "iex") -> list[dict]:
        """Most recent `limit` completed-or-current daily bars, oldest first."""
        start = (dt.date.today() - dt.timedelta(days=int(limit * 1.9) + 30)).isoformat()
        bars, token = [], None
        while True:
            params = {
                "timeframe": "1Day", "limit": 1000, "adjustment": adjustment,
                "feed": feed, "sort": "asc", "start": start,
            }
            if token:
                params["page_token"] = token
            resp = self._request(self.data_base, "GET",
                                 f"/v2/stocks/{symbol}/bars", params)
            bars += resp.get("bars") or []
            token = resp.get("next_page_token")
            if not token:
                break
        return bars[-limit:]

    # -------------------------------------------------------------- trading
    def clock(self) -> dict:
        return self._request(self.trading_base, "GET", "/v2/clock")

    def calendar(self, start, end):
        return self._request(self.trading_base, "GET", "/v2/calendar", {"start": start, "end": end})

    def open_orders(self):
        orders = self._request(self.trading_base, "GET", "/v2/orders",
                               {"status": "open", "limit": 500})
        if len(orders) >= 500:
            raise AlpacaError("open order response at limit; refusing incomplete reconciliation")
        return orders

    def order(self, order_id):
        return self._request(self.trading_base, "GET", f"/v2/orders/{order_id}")

    def order_by_client_id(self, client_id):
        try:
            return self._request(self.trading_base, "GET",
                                 "/v2/orders:by_client_order_id",
                                 {"client_order_id": client_id})
        except AlpacaError as exc:
            if exc.status == 404:
                return None
            raise

    def latest_trade(self, symbol):
        return self._request(self.data_base, "GET", f"/v2/stocks/{symbol}/trades/latest",
                             {"feed": "iex"})["trade"]

    def account(self) -> dict:
        return self._request(self.trading_base, "GET", "/v2/account")

    def asset(self, symbol) -> dict:
        return self._request(self.trading_base, "GET", f"/v2/assets/{symbol}")

    def positions(self) -> list[dict]:
        return self._request(self.trading_base, "GET", "/v2/positions")

    def submit_order(self, symbol: str, qty: int, side: str,
                     client_order_id: str | None = None) -> dict:
        body = {
            "symbol": symbol, "qty": str(qty), "side": side,
            "type": "market", "time_in_force": "day",
        }
        if client_order_id:
            body["client_order_id"] = client_order_id
        return self._request(self.trading_base, "POST", "/v2/orders", body=body)
