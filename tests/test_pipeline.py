import copy
import datetime as dt
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import pandas as pd
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "aws" / "src"))
from shared import experiment as exp, runtime, state
import signal_handler
import reporter_handler
import executor_handler


class Table:
    def __init__(self):
        self.items = {}

    def get_item(self, Key, **kwargs):
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": copy.deepcopy(item)} if item else {}

    def put_item(self, Item, **kwargs):
        key = (Item["pk"], Item["sk"])
        if key in self.items and kwargs.get("ConditionExpression"):
            expired = kwargs.get("ExpressionAttributeValues", {}).get(":now", 0) > self.items[key].get("expires", float("inf"))
            if not expired:
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[key] = copy.deepcopy(Item)

    def delete_item(self, Key, **kwargs):
        key = (Key["pk"], Key["sk"])
        if kwargs.get("ConditionExpression") and self.items.get(key, {}).get("owner") != kwargs["ExpressionAttributeValues"][":owner"]:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "DeleteItem")
        self.items.pop(key, None)

    def scan(self, **kwargs):
        return {"Items": copy.deepcopy(list(self.items.values()))}

    def query(self, KeyConditionExpression, **kwargs):
        partition = KeyConditionExpression.get_expression()["values"][1]
        return {"Items": [copy.deepcopy(v) for (pk, sk), v in sorted(self.items.items()) if pk == partition]}


class Broker:
    def __init__(self):
        dates = pd.bdate_range(end="2026-08-11", periods=325)
        self.bars = {s: [{"t": str(d.date())+"T04:00:00Z", "c": 10+i*.03} for i, d in enumerate(dates)]
                     for s in ["A", "B", "C", "D"]}
        self.pos, self.orders = {}, []
        self.cash = 100000
        self.fill = "filled"
        self.error_after_submit = False
        self.open = True

    def daily_bars(self, symbol, **kw):
        return copy.deepcopy(self.bars[symbol])

    def calendar(self, start, end):
        return [dict(date=str(d.date()), open="09:30", close="16:00") for d in pd.bdate_range(start, end)]

    def clock(self):
        return {"is_open": self.open}

    def account(self):
        return dict(status="ACTIVE", cash=str(self.cash), equity="100000")

    def positions(self):
        return [dict(symbol=s, qty=str(q)) for s, q in self.pos.items() if q]

    def latest_trade(self, symbol):
        return dict(p=20, t=dt.datetime.now(dt.timezone.utc).isoformat())

    def asset(self, symbol):
        return dict(symbol=symbol, status="active", tradable=True)

    def open_orders(self):
        return [o for o in self.orders if o["status"] in {"new", "partially_filled"}]

    def order(self, order_id):
        return next(o for o in self.orders if o["id"] == order_id)

    def order_by_client_id(self, client_id):
        return next((o for o in self.orders if o["client_order_id"] == client_id), None)

    def submit_order(self, symbol, qty, side, client_order_id):
        if self.order_by_client_id(client_order_id):
            raise ValueError("duplicate client id")
        filled = qty if self.fill == "filled" else 1 if self.fill == "partially_filled" else 0
        self.pos[symbol] = self.pos.get(symbol, 0) + (filled if side == "buy" else -filled)
        order = dict(id=f"o{len(self.orders)}", symbol=symbol, qty=qty, side=side, status=self.fill,
                     filled_qty=filled, filled_avg_price="20" if filled else None,
                     client_order_id=client_order_id, submitted_at=dt.datetime.now(dt.timezone.utc).isoformat())
        self.orders.append(order)
        if self.error_after_submit:
            self.error_after_submit = False
            raise TimeoutError("response lost after acceptance")
        return order


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.table, self.broker = Table(), Broker()
        self.patches = [patch.dict(os.environ, {"BOOK_BUDGET_USD": "12000", "COST_BPS": "5", "EXPERIMENT_ID": "test", "DRY_RUN": "true", "MAX_ORDER_USD": "100000", "DASHBOARD_BUCKET": "fake"}),
                        patch.object(runtime, "_table", self.table), patch.object(runtime, "_alpaca", self.broker),
                        patch.object(runtime, "get_watchlist", return_value=["A", "B", "C", "D"]),
                        patch.object(exp, "today", return_value="2026-08-10")]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.objects = {}
        class S3:
            def put_object(inner, **kw):
                self.objects[kw["Key"]] = kw["Body"]
        p = patch.object(runtime, "_s3", S3())
        p.start()
        self.addCleanup(p.stop)

    def signal(self):
        return signal_handler.lambda_handler({}, None)

    def execute(self):
        return executor_handler.lambda_handler({}, None)

    def test_seven_books_atomic_publish_and_no_rewrite(self):
        first = self.signal()
        self.assertEqual(len(first["targets"]), 28)
        self.broker.bars["A"][-2]["c"] *= 3
        self.assertEqual(self.signal()["run_id"], first["run_id"])

    def test_partial_data_does_not_publish_any_targets(self):
        self.broker.bars["B"] = []
        with self.assertRaises(ValueError):
            self.signal()
        self.assertIsNone(exp.get(self.table, "RUN", exp.today()))
        self.assertIn("no complete batch", self.execute()["skipped"])

    def test_ledger_preserves_legacy_and_reruns(self):
        state.put_snap(self.table, "tsmom_126", "2026-08-01", {}, {}, 0, 500)
        self.signal()
        reporter_handler.lambda_handler({}, None)
        original = exp.get(self.table, "LEDGER", exp.today())
        self.assertEqual(len(original["books"]), 7)
        for b in original["books"].values():
            self.assertAlmostEqual(b["equity"], b["cash"]+b["capital"])
            self.assertAlmostEqual(b["cum_ret"], b["equity"]/12000-1)
        self.broker.bars["A"][-2]["c"] *= 2
        reporter_handler.lambda_handler({}, None)
        self.assertEqual(original, exp.get(self.table, "LEDGER", exp.today()))
        self.assertEqual(len(state.get_snaps(self.table, "tsmom_126")), 1)
        self.assertEqual(json.loads(self.objects["data.json"])["schema_version"], 2)

    def test_missing_close_and_missing_session_fail_without_writes(self):
        self.signal()
        self.broker.bars["A"] = self.broker.bars["A"][:-2]
        with self.assertRaises(ValueError):
            reporter_handler.lambda_handler({}, None)
        self.assertIsNone(exp.get(self.table, "LEDGER", exp.today()))

    def test_next_day_buyhold_units_persist(self):
        self.signal()
        reporter_handler.lambda_handler({}, None)
        first = exp.get(self.table, "LEDGER", exp.today())
        with patch.object(exp, "today", return_value="2026-08-11"):
            self.signal()
            reporter_handler.lambda_handler({}, None)
            second = exp.get(self.table, "LEDGER", exp.today())
        self.assertEqual(first["books"]["buy_and_hold"]["positions"], second["books"]["buy_and_hold"]["positions"])
        self.assertEqual(len(json.loads(self.objects["data.json"])["strategies"][0]["series"]), 2)

    def test_dry_run_and_cap(self):
        self.signal()
        self.assertTrue(any(a.get("dry_run") for a in self.execute()["actions"]))
        self.assertFalse(self.broker.orders)
        with patch.dict(os.environ, {"MAX_ORDER_USD": "1"}):
            self.assertTrue(all("blocked" in a for a in self.execute()["actions"]))

    def test_pending_and_partial_orders_block_duplicates(self):
        self.signal()
        self.broker.fill = "partially_filled"
        with patch.dict(os.environ, {"DRY_RUN": "false"}):
            self.execute()
            count = len(self.broker.orders)
            self.assertGreater(count, 0)
            self.assertIn("open orders", self.execute()["skipped"])
            self.assertEqual(len(self.broker.orders), count)
        for key, item in self.table.items.items():
            if key[0].startswith("BOTPOS#"):
                self.assertEqual(item["shares"], 1)

    def test_rejected_order_not_retried_with_new_id(self):
        self.signal()
        self.broker.fill = "rejected"
        with patch.dict(os.environ, {"DRY_RUN": "false"}):
            self.execute()
            count = len(self.broker.orders)
            self.execute()
            self.assertEqual(count, len(self.broker.orders))
        self.assertFalse(any(k[0].startswith("BOTPOS#") for k in self.table.items))

    def test_uncertain_response_lookup_prevents_duplicate(self):
        self.signal()
        self.broker.error_after_submit = True
        with patch.dict(os.environ, {"DRY_RUN": "false"}):
            with self.assertRaises(TimeoutError):
                self.execute()
            self.execute()
            ids = [o["client_order_id"] for o in self.broker.orders]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertEqual(len(state.get_trades(self.table)), len(self.broker.orders))

    def test_insufficient_cash_and_changed_watchlist_block(self):
        self.signal()
        self.broker.cash = 1
        result = self.execute()
        self.assertTrue(all("cash" in a.get("blocked", "") for a in result["actions"]))
        with patch.object(runtime, "get_watchlist", return_value=["A"]):
            self.assertIn("watchlist", self.execute()["skipped"])

    def test_stale_batch_does_not_liquidate(self):
        self.signal()
        key = ("RUN#test", exp.today())
        run = json.loads(self.table.items[key]["payload"])
        run["ts"] = "2020-01-01T00:00:00+00:00"
        self.table.items[key]["payload"] = json.dumps(run)
        self.broker.pos["A"] = 10
        state.put_bot_position(self.table, "A", 10, run["ts"])
        self.assertIn("stale", self.execute()["skipped"])
        self.assertFalse(self.broker.orders)

    def test_executor_lease_blocks_overlap_and_recovers_expiry(self):
        self.signal()
        owner = exp.acquire_executor(self.table)
        self.assertIn("lease", self.execute()["skipped"])
        exp.release_executor(self.table, "wrong-owner")
        self.assertIsNone(exp.acquire_executor(self.table))
        self.table.items[("LOCK#paper-executor", "latest")]["expires"] = 0
        replacement = exp.acquire_executor(self.table)
        self.assertIsNotNone(replacement)
        exp.release_executor(self.table, owner)
        self.assertIsNone(exp.acquire_executor(self.table))
        exp.release_executor(self.table, replacement)
        self.assertTrue(self.execute()["dry_run"])

    def test_reporter_refreshes_fills_that_complete_after_execution(self):
        self.signal()
        self.broker.fill = "partially_filled"
        with patch.dict(os.environ, {"DRY_RUN": "false"}):
            self.execute()
        count = len(self.broker.orders)
        for order in self.broker.orders:
            order.update(status="filled", filled_qty=order["qty"], filled_avg_price="20.15")
        reporter_handler.lambda_handler({}, None)
        data = json.loads(self.objects["data.json"])
        self.assertTrue(data["orders_refreshed"])
        self.assertEqual(len(data["trades"]), count)
        self.assertTrue(all(t["status"] == "filled" and t["filled_qty"] == t["qty"] and t["price"] == 20.15 for t in data["trades"]))
        self.assertEqual(len(self.broker.orders), count)
        self.assertFalse(state.pending_trades(self.table))

    def test_reporter_respects_executor_lease(self):
        self.signal()
        owner = exp.acquire_executor(self.table)
        with patch.object(state, "refresh_orders") as refresh:
            reporter_handler.lambda_handler({}, None)
            refresh.assert_not_called()
        self.assertFalse(json.loads(self.objects["data.json"])["orders_refreshed"])
        self.assertIsNone(exp.acquire_executor(self.table))
        exp.release_executor(self.table, owner)
        reporter_handler.lambda_handler({}, None)
        self.assertTrue(json.loads(self.objects["data.json"])["orders_refreshed"])

    def test_order_refresh_failure_keeps_ledger_and_flags_stale_orders(self):
        self.signal()
        with patch.object(state, "refresh_orders", side_effect=TimeoutError("broker unavailable")):
            reporter_handler.lambda_handler({}, None)
        data = json.loads(self.objects["data.json"])
        self.assertFalse(data["orders_refreshed"])
        self.assertEqual(len(data["strategies"]), 7)
        owner = exp.acquire_executor(self.table)
        self.assertIsNotNone(owner)
        exp.release_executor(self.table, owner)

    def test_suspended_account_and_untradable_asset_place_no_orders(self):
        self.signal()
        account = dict(self.broker.account(), trade_suspended_by_user=True)
        with patch.dict(os.environ, {"DRY_RUN": "false"}):
            with patch.object(self.broker, "account", return_value=account):
                self.assertIn("account", self.execute()["skipped"])
            with patch.object(self.broker, "asset", return_value={"status": "inactive", "tradable": False}):
                self.assertIn("asset", self.execute()["skipped"])
        self.assertFalse(self.broker.orders)

    def test_unmanaged_positions_are_not_adopted_or_liquidated(self):
        self.signal()
        with patch.dict(os.environ, {"DRY_RUN": "false"}):
            for symbol in ["A", "OUTSIDE"]:
                with self.subTest(symbol=symbol):
                    self.broker.pos = {symbol: 10}
                    self.assertIn("unmanaged", self.execute()["skipped"])
        self.assertFalse(self.broker.orders)
        self.assertFalse(state.get_bot_symbols(self.table))

    def test_live_endpoint_and_stale_quotes_block_orders(self):
        self.signal()
        with patch.dict(os.environ, {"DRY_RUN": "false"}):
            with patch.object(self.broker, "trading_base", "https://api.alpaca.markets", create=True):
                with self.assertRaisesRegex(ValueError, "paper-only"):
                    self.execute()
            with patch.object(self.broker, "latest_trade", return_value={"p": 20, "t": "2020-01-01T00:00:00Z"}):
                self.assertIn("stale execution price", self.execute()["skipped"])
        self.assertFalse(self.broker.orders)

    def test_sales_do_not_fund_buys_within_the_same_invocation(self):
        self.signal()
        self.broker.pos = {"A": 2000}
        self.broker.cash = 1
        state.put_bot_position(self.table, "A", 2000, "2026-08-10")
        with patch.dict(os.environ, {"DRY_RUN": "false"}):
            result = self.execute()
        self.assertTrue(self.broker.orders)
        self.assertTrue(all(o["side"] == "sell" for o in self.broker.orders))
        self.assertTrue(any(a.get("blocked") == "insufficient unreserved cash" for a in result["actions"]))

    def test_mismatched_saved_plan_is_rejected(self):
        self.signal()
        exp.put_once(self.table, "PLAN", exp.today(), {"run_id": "wrong", "desired": {"A": 5}})
        with patch.dict(os.environ, {"DRY_RUN": "false"}):
            with self.assertRaisesRegex(ValueError, "saved plan"):
                self.execute()
        self.assertFalse(self.broker.orders)

    def test_broker_timestamp_revision_does_not_duplicate_order(self):
        order = dict(id="stable-id", status="pending_new", qty="10", filled_qty="0",
                     submitted_at="2026-08-10T19:45:00.123000Z")
        state.record_trade(self.table, "A", "buy", 10, order, order["submitted_at"])
        order.update(status="filled", filled_qty="10", filled_avg_price="20.1",
                     submitted_at="2026-08-10T19:45:00.124567Z")
        state.record_trade(self.table, "A", "buy", 10, order, order["submitted_at"])
        trades = state.get_trades(self.table)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["status"], "filled")
        self.assertEqual(trades[0]["sk"], order["submitted_at"])
        self.assertFalse(state.pending_trades(self.table))
        self.assertEqual(len(self.table.items), 1)

    def test_legacy_order_duplicates_are_preserved_but_counted_once(self):
        original = dict(pk="ORDER#stable-id", sk="2026-08-10T19:45:00.123Z",
                        order_id="stable-id", symbol="A", side="buy", qty=10,
                        status="pending_new", filled_qty=0)
        self.table.put_item(Item=original)
        filled = dict(original, sk="2026-08-10T19:45:00.124Z", status="filled", filled_qty=10)
        self.table.put_item(Item=filled)
        self.assertEqual(len(state.get_trades(self.table)), 1)
        self.assertEqual(state.get_trades(self.table)[0]["filled_qty"], 10)
        self.assertFalse(state.pending_trades(self.table))
        self.assertEqual(len(self.table.items), 2)
        # A new observation uses a stable key and supersedes legacy copies.
        order = dict(id="stable-id", status="filled", filled_qty=10, filled_avg_price="20.1")
        state.record_trade(self.table, "A", "buy", 10, order, filled["sk"])
        self.assertEqual(len(state.get_trades(self.table)), 1)
        self.assertEqual(len(self.table.items), 3)

    # ---- a ticker whose data or price is late must not cancel the whole day

    def test_symbol_one_session_late_is_used_and_recorded(self):
        # The previous session for 2026-08-10 is 2026-08-07; B's bar for it is missing.
        self.broker.bars["B"] = [b for b in self.broker.bars["B"] if b["t"][:10] != "2026-08-07"]
        self.signal()
        run = exp.get(self.table, "RUN", exp.today())
        self.assertEqual(run["late_symbols"], ["B"])
        self.assertEqual(len(run["targets"]), 28)

    def test_symbol_two_sessions_late_still_blocks_the_batch(self):
        self.broker.bars["B"] = [b for b in self.broker.bars["B"]
                                 if b["t"][:10] not in ("2026-08-06", "2026-08-07")]
        with self.assertRaisesRegex(ValueError, "incomplete history"):
            self.signal()
        self.assertIsNone(exp.get(self.table, "RUN", exp.today()))

    def test_one_unpriced_symbol_waits_while_others_trade(self):
        self.signal()
        priced = self.broker.latest_trade

        def latest_trade(symbol):
            return {"p": 20, "t": "2020-01-01T00:00:00Z"} if symbol == "B" else priced(symbol)

        with patch.dict(os.environ, {"DRY_RUN": "false"}), \
                patch.object(self.broker, "latest_trade", side_effect=latest_trade):
            result = self.execute()
        self.assertIn({"symbol": "B", "waiting": "missing/stale execution price"},
                      result["actions"])
        traded = {o["symbol"] for o in self.broker.orders}
        self.assertTrue(traded)
        self.assertNotIn("B", traded)

    # ---- broker lots are carried forward instead of re-rounded every day

    def _trade_two_days(self, day_two_price):
        with patch.dict(os.environ, {"DRY_RUN": "false"}):
            self.signal()
            self.execute()
            day_one_orders = len(self.broker.orders)
            with patch.object(exp, "today", return_value="2026-08-11"):
                self.signal()
                runs = [exp.get(self.table, "RUN", d)["books"] for d in ("2026-08-10", "2026-08-11")]
                self.assertEqual(*[{name: book["weights"] for name, book in b.items()}
                                   for b in runs], "precondition: same signals both days")
                with patch.object(self.broker, "latest_trade",
                                  return_value={"p": day_two_price,
                                                "t": dt.datetime.now(dt.timezone.utc).isoformat()}):
                    self.execute()
        return self.broker.orders[day_one_orders:]

    def test_price_move_inside_band_places_no_orders(self):
        self.assertEqual(self._trade_two_days(20.6), [])  # +3%: rounding alone would trade

    def test_price_move_outside_band_rebalances(self):
        orders = self._trade_two_days(26.0)  # +30%: positions now well above target
        self.assertTrue(orders)
        self.assertTrue(all(o["side"] == "sell" for o in orders))


class SizeLotsTests(unittest.TestCase):
    """executor_handler.size_lots: 12,000 budget, so a 1/12 weight targets $1,000."""

    W = 1 / 12

    def run_with(self, weights, buy_and_hold=None):
        books = {"tsmom": dict(weights=weights, buy_and_hold=False)}
        if buy_and_hold:
            books["buy_and_hold"] = dict(weights=buy_and_hold, buy_and_hold=True)
        return dict(config=dict(budget=12000), books=books)

    def lots(self, weights, prices, previous=None, **kw):
        result = executor_handler.size_lots(self.run_with(weights, **kw), prices, previous or {})
        return {s: lot["shares"] for s, lot in result.get("tsmom", {}).items()}

    def carried(self, shares, weight=W):
        return {"tsmom": {"X": {"weight": weight, "shares": shares}}}

    def test_new_lot_is_sized_with_the_price_buffer(self):
        self.assertEqual(self.lots({"X": self.W}, {"X": 20}), {"X": 49})  # 1000 / 20.40

    def test_carried_lot_kept_while_value_stays_in_band(self):
        # Fresh sizing at 20.6 would give 47 shares; the carried 49 are within 20%.
        self.assertEqual(self.lots({"X": self.W}, {"X": 20.6}, self.carried(49)), {"X": 49})

    def test_carried_lot_resized_once_value_leaves_band(self):
        self.assertEqual(self.lots({"X": self.W}, {"X": 26}, self.carried(49)), {"X": 37})

    def test_exits_entries_and_weight_changes(self):
        self.assertEqual(self.lots({"X": 0}, {"X": 20}, self.carried(49)), {})
        self.assertEqual(self.lots({"X": self.W, "Y": self.W}, {"X": 20, "Y": 50},
                                   self.carried(49)), {"X": 49, "Y": 19})
        self.assertEqual(self.lots({"X": self.W}, {"X": 20}, self.carried(150, weight=1 / 3)),
                         {"X": 49})

    def test_unaffordable_zero_share_lot_is_retried(self):
        self.assertEqual(self.lots({"X": self.W}, {"X": 990}), {"X": 0})
        self.assertEqual(self.lots({"X": self.W}, {"X": 950}, self.carried(0)), {"X": 1})

    def test_unpriced_symbol_keeps_existing_lot_only(self):
        self.assertEqual(self.lots({"X": self.W}, {}, self.carried(49)), {"X": 49})
        self.assertEqual(self.lots({"X": self.W}, {}), {})

    def test_buy_and_hold_benchmark_gets_no_broker_lots(self):
        run = self.run_with({"X": self.W}, buy_and_hold={"X": 1.0})
        self.assertNotIn("buy_and_hold", executor_handler.size_lots(run, {"X": 20}, {}))


if __name__ == "__main__":
    unittest.main()
