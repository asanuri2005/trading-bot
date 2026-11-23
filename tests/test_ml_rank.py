"""Version-2 ML books: panel features, the numpy tree evaluator, and MLRanker."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ml_rank
from strategies import PANEL_FEATURES, MLRanker, TreeEnsembleModel, compute_panel_features

try:
    import lightgbm  # noqa: F401  (training-only dependency)
    HAVE_LIGHTGBM = True
except ImportError:
    HAVE_LIGHTGBM = False


def random_walks(n_symbols=8, n_days=420, seed=3):
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2020-01-01", periods=n_days)
    return {f"S{i}": pd.DataFrame({"close": 50 * np.exp(np.cumsum(rng.normal(0.0003, 0.02, n_days)))},
                                  index=index) for i in range(n_symbols)}


class PanelFeatureTests(unittest.TestCase):
    def test_features_use_no_future_prices(self):
        prices = random_walks()
        cutoff = prices["S0"].index[350]
        original = compute_panel_features(prices)
        changed = {s: px.copy() for s, px in prices.items()}
        for px in changed.values():
            px.loc[px.index > cutoff, "close"] *= 3  # rewrite the future only
        after = compute_panel_features(changed)
        dates = original.index.get_level_values("date")
        pd.testing.assert_frame_equal(original[dates <= cutoff], after[after.index.get_level_values("date") <= cutoff])

    def test_late_listing_joins_once_it_has_history(self):
        prices = random_walks()
        prices["NEW"] = prices["S1"].iloc[150:].copy()  # lists 150 sessions late
        panel = compute_panel_features(prices)
        first = panel.xs("NEW", level="symbol").index.min()
        # The 200-day average first exists on the 200th bar, so 199 bars come before it.
        self.assertEqual((prices["NEW"].index < first).sum(), 199)
        self.assertTrue(np.isfinite(panel.to_numpy()).all())

    def test_relative_label_splits_each_day_near_half(self):
        data = ml_rank.build_dataset(random_walks(), horizon=5)
        share = data.dropna(subset=["y"]).groupby(level="date")["y"].mean()
        self.assertTrue(share.between(0.3, 0.7).all())  # 8 stocks: 3, 4 or 5 beat the median


class TreeEvaluatorTests(unittest.TestCase):
    @unittest.skipUnless(HAVE_LIGHTGBM, "lightgbm not installed")
    def test_numpy_trees_reproduce_lightgbm(self):
        rng = np.random.default_rng(0)
        X = rng.normal(size=(4000, len(PANEL_FEATURES)))
        y = (X[:, 0] + 0.5 * X[:, 3] * X[:, 5] + rng.normal(size=4000) > 0).astype(float)
        exported, fitted = ml_rank.fit_trees(X, y)
        ours = TreeEnsembleModel(exported).predict(X)
        np.testing.assert_allclose(ours, fitted.predict_proba(X)[:, 1], atol=1e-9)

    def test_hand_built_tree(self):
        # Root splits on feature 0 at 1.0; left leaf +2, right leaf -2.
        model = TreeEnsembleModel({"trees": [dict(feature=[0, -1, -1], threshold=[1.0, 0, 0],
                                                  left=[1, -1, -1], right=[2, -1, -1], value=[0, 2.0, -2.0])]})
        p = model.predict(np.array([[0.5] + [0] * 19, [1.0] + [0] * 19, [1.5] + [0] * 19]))
        np.testing.assert_allclose(p, 1 / (1 + np.exp(-np.array([2.0, 2.0, -2.0]))))  # 1.0 <= 1.0 goes left


class RankerTests(unittest.TestCase):
    def ranker(self, top_n=3, smooth=5):
        # A logistic model that only looks at a stock's 21-day return rank.
        coef = [0.0] * len(PANEL_FEATURES)
        coef[PANEL_FEATURES.index("xs_r21")] = 5.0
        model = dict(type="logistic_regression", features=PANEL_FEATURES, mu=[0.0] * len(coef),
                     sigma=[1.0] * len(coef), coef=coef, intercept=0.0)
        directory = tempfile.mkdtemp()
        Path(directory, "m.json").write_text(json.dumps(model))
        return MLRanker("m.json", "ml_test", top_n=top_n, smooth=smooth, model_dir=directory)

    def test_holds_exactly_top_n_by_recent_strength(self):
        prices = random_walks()
        ranker = self.ranker()
        signals = ranker.universe_signals(prices)
        self.assertEqual(sum(signals.values()), 3)
        best = ranker.scores(prices).sort_values(ascending=False).index[:3]
        self.assertEqual({s for s, v in signals.items() if v}, set(best))

    def test_stock_without_todays_features_is_not_eligible(self):
        prices = random_walks()
        prices["S0"] = prices["S0"].iloc[:-1]  # no bar for the latest session
        self.assertEqual(self.ranker(top_n=8).universe_signals(prices)["S0"], 0)

    def test_mismatched_feature_list_is_refused(self):
        directory = tempfile.mkdtemp()
        Path(directory, "old.json").write_text(json.dumps({"type": "logistic_regression", "features": ["r1"]}))
        with self.assertRaisesRegex(ValueError, "retrain"):
            MLRanker("old.json", "x", model_dir=directory)


class WalkForwardTests(unittest.TestCase):
    def test_each_fit_sees_only_labels_resolved_before_its_quarter(self):
        data = ml_rank.build_dataset(random_walks(n_days=700), horizon=5)
        sizes = []

        def fake_fit(X, y):
            sizes.append(len(y))
            return dict(type="logistic_regression", mu=[0.0] * X.shape[1], sigma=[1.0] * X.shape[1],
                        coef=[0.0] * X.shape[1], intercept=0.0)

        with patch.object(ml_rank, "fit_logistic", side_effect=fake_fit), \
                patch.object(ml_rank, "fit_trees", side_effect=lambda X, y: (fake_fit(X, y), None)):
            ml_rank.walk_forward(data, "2021-07-01", min_train=1)
        labeled = data.dropna(subset=["y", "label_date"])
        dates = data.index.get_level_values("date")
        cutoffs = pd.date_range("2021-07-01", dates.max(), freq="QS")
        expected = [int((labeled["label_date"] < c).sum()) for c in cutoffs]
        self.assertEqual(sizes[0::2], expected)  # logistic fits; trees fit on the same rows
        self.assertEqual(sizes[1::2], expected)


if __name__ == "__main__":
    unittest.main()
