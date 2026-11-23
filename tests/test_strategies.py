"""The buffered rotation and the breadth filter."""
from pathlib import Path
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategies import BreadthFilter, BufferedRotation


def trending(rates, days=260):
    """One stock per daily growth rate; a higher rate ranks higher on momentum."""
    index = pd.bdate_range("2025-01-01", periods=days)
    return {f"S{i:02d}": pd.DataFrame({"close": 100 * (1 + r) ** np.arange(days)}, index=index)
            for i, r in enumerate(rates)}


class BufferedRotationTests(unittest.TestCase):
    def setUp(self):
        self.prices = trending([0.0001 * i for i in range(12)])  # S11 strongest, S00 weakest
        self.rotation = BufferedRotation(top_n=5, keep_until=10)

    def held(self, previous=None):
        return {s for s, v in self.rotation.universe_signals(self.prices, previous=previous).items() if v}

    def test_starts_from_the_top_five(self):
        self.assertEqual(self.held(), {"S11", "S10", "S09", "S08", "S07"})

    def test_keeps_a_holding_ranked_tenth_or_better(self):
        # S03 ranks 9th: inside the buffer, so it stays and takes one of the five slots.
        self.assertEqual(self.held({"S03"}), {"S03", "S11", "S10", "S09", "S08"})

    def test_drops_a_holding_ranked_below_tenth(self):
        # S01 ranks 11th: outside the buffer, so the book refills from the top.
        self.assertEqual(self.held({"S01"}), {"S11", "S10", "S09", "S08", "S07"})

    def test_never_holds_more_than_top_n(self):
        self.assertEqual(len(self.held({"S02", "S03", "S04", "S05", "S06", "S07"})), 5)


class BreadthFilterTests(unittest.TestCase):
    def test_holds_everything_only_when_most_stocks_are_above_their_average(self):
        index = pd.bdate_range("2025-01-01", periods=220)
        up = pd.DataFrame({"close": np.linspace(50, 100, 220)}, index=index)
        down = pd.DataFrame({"close": np.linspace(100, 50, 220)}, index=index)
        breadth = BreadthFilter()
        on = breadth.universe_signals({"A": up, "B": up, "C": down})
        off = breadth.universe_signals({"A": up, "B": down, "C": down})
        self.assertEqual(on, {"A": 1, "B": 1, "C": 1})  # 2 of 3 above
        self.assertEqual(off, {"A": 0, "B": 0, "C": 0})  # 1 of 3 above
        self.assertEqual(breadth.universe_signals({"A": up, "B": down}), {"A": 0, "B": 0})  # exactly half


if __name__ == "__main__":
    unittest.main()
