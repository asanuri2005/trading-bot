import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "aws" / "src"))
from shared.portfolio import advance


class AccountingTests(unittest.TestCase):
    def test_cash_counts_in_return_and_ranking(self):
        small = advance(None, {"A": 20}, {"A": .2}, 10000, 0)
        small = advance(small, {"A": 21}, {}, 10000, 0, hold=True)
        full = advance(None, {"A": 20}, {"A": 1}, 10000, 0)
        full = advance(full, {"A": 20.4}, {}, 10000, 0, hold=True)
        self.assertAlmostEqual(small["cum_ret"], .01)
        self.assertAlmostEqual(full["cum_ret"], .02)
        self.assertAlmostEqual(small["cash"], 8000)

    def test_cash_only_and_cost_conservation(self):
        book = advance(None, {"A": 20}, {"A": 1}, 1000, 5)
        self.assertGreaterEqual(book["cash"], 0)
        self.assertAlmostEqual(book["equity"], 1000-book["costs"])
        flat = advance(book, {"A": 20}, {"A": 0}, 1000, 5)
        self.assertFalse(flat["positions"])
        self.assertAlmostEqual(flat["cash"], book["equity"]-flat["costs"])
        again = advance(flat, {"A": 30}, {"A": 0}, 1000, 5)
        self.assertAlmostEqual(again["day_ret"], 0)

    def test_buyhold_never_rebalances(self):
        book = advance(None, {"A": 20, "B": 40}, {"A": .5, "B": .5}, 1000, 5)
        following = advance(book, {"A": 40, "B": 40}, {"B": 1}, 1000, 5, hold=True)
        self.assertEqual(book["positions"], following["positions"])
        self.assertEqual(following["costs"], 0)

    def test_adjustment_revisions_preserve_total_return(self):
        book = advance(None, {"A": 20}, {"A": 1}, 1000, 0)
        following = advance(book, {"A": 11}, {}, 1000, 0, hold=True, previous_prices={"A": 10})
        self.assertAlmostEqual(following["cum_ret"], .1)

    def test_invalid_prices_and_overweight_fail(self):
        for price in [0, float("nan"), -2]:
            with self.assertRaises(ValueError):
                advance(None, {"A": price}, {"A": 1})
        with self.assertRaises(ValueError):
            advance(None, {"A": 20}, {"A": 1.1})


if __name__ == "__main__":
    unittest.main()
