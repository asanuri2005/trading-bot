from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backtest import _trade_stats
from ml_train import build_rows, evaluate_oos, walk_forward
from portfolio_backtest import run_portfolio
from strategies import BuyAndHold, CrossSectionalMomentum, TimeSeriesMomentum


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.prices = pd.DataFrame({"close": np.linspace(10, 30, 450)}, index=pd.bdate_range("2020-01-01", periods=450))

    def test_tsmom_has_warmup_and_ml_has_no_unscored_tail(self):
        index = self.prices.index[250:400]
        predictions = pd.Series(.6, index=index)
        ml, ts = evaluate_oos(self.prices, predictions, cost_bps=0)
        self.assertEqual(ml.index[-1], index[-1])
        self.assertTrue(ml.index.equals(ts.index))
        self.assertTrue((ts.position.iloc[1:] == 1).all())
        np.testing.assert_allclose(ml.equity, ts.equity)

    def test_prediction_gap_is_not_silently_cash(self):
        p = pd.Series(.6, index=self.prices.index[250:400]).drop(self.prices.index[300])
        with self.assertRaisesRegex(ValueError, "Missing OOS"):
            evaluate_oos(self.prices, p)

    def test_unresolved_labels_excluded_from_training(self):
        rows = build_rows(self.prices, "A", 5)
        calls = []
        def fit(X, y, **kw):
            calls.append(len(y))
            return np.zeros(X.shape[1]), 0
        cutoffs = pd.date_range("2021-01-01", rows.date.max(), freq="QS")
        with patch("ml_train.fit_logreg", side_effect=fit):
            result = walk_forward(rows, "2021-01-01", 1, 1)
        expected = [int((rows.label_date < cut).sum()) for cut in cutoffs]
        self.assertEqual(calls, expected)
        self.assertGreater(len(result), 0)

    def test_no_training_window_gives_actionable_error(self):
        rows = build_rows(self.prices, "A", 5)
        with self.assertRaisesRegex(ValueError, "No eligible"):
            walk_forward(rows, "2021-01-01", 1, 100000)

    def test_exit_cost_and_open_trade_count(self):
        frame = pd.DataFrame({"position": [0, 1, 1, 0, 1], "strat_ret": [0, .001, .001, -.01, .5]})
        count, win = _trade_stats(frame)
        self.assertEqual(count, 1)
        self.assertEqual(win, 0)

    def test_rotation_backtest_uses_only_prior_history(self):
        assets = {"A": self.prices.copy(), "B": self.prices.copy()*1.5}
        strategy = CrossSectionalMomentum(top_n=1)
        original = run_portfolio(assets, strategy, cost_bps=0)
        cutoff = self.prices.index[400]
        assets["B"].loc[assets["B"].index > cutoff, "close"] *= 100
        changed = run_portfolio(assets, strategy, cost_bps=0)
        np.testing.assert_allclose(original.loc[:cutoff, "equity"], changed.loc[:cutoff, "equity"])
        self.assertGreaterEqual(changed.cash.min(), 0)

    def test_portfolio_entry_does_not_earn_prior_move(self):
        result = run_portfolio({"A": self.prices}, BuyAndHold(), cost_bps=0)
        self.assertEqual(result.day_ret.iloc[0], 0)
        expected = self.prices.close.iloc[-1]/self.prices.close.loc[result.index[0]]-1
        self.assertAlmostEqual(result.cum_ret.iloc[-1], expected)


if __name__ == "__main__":
    unittest.main()
