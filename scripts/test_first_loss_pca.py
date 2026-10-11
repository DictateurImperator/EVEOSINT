"""PCA dimensionality, cohort diagnostics and time-varying return associations."""

import unittest
import numpy as np
from scripts.first_loss_study.pca import decompose
from scripts.first_loss_study.model import yearly_associations, adjusted_association


class PcaTests(unittest.TestCase):
    def test_variance_target_keeps_third_axis_and_full_coordinates_reconstruct(self):
        points = np.concatenate(
            (
                np.diag(np.sqrt(3 * np.array([4.0, 3.0, 3.0]))),
                -np.diag(np.sqrt(3 * np.array([4.0, 3.0, 3.0]))),
            )
        )
        mean, values, axes, summary = decompose(
            len(points), points.sum(axis=0), points.T @ points, ["a", "b", "c"]
        )
        self.assertEqual(summary["components_retained"], 3)
        self.assertAlmostEqual(summary["spectrum"][1]["cumulative_ratio"], 0.7)
        self.assertAlmostEqual(summary["spectrum"][2]["cumulative_ratio"], 1)
        np.testing.assert_allclose(
            ((points - mean) @ axes) @ axes.T + mean, points, atol=1e-12
        )
        self.assertEqual(len(summary["spectrum"]), 3)

    def test_target_is_not_capped_at_sixteen_axes(self):
        mean, values, axes, summary = decompose(
            100, np.zeros(30), 100 * np.eye(30), [str(i) for i in range(30)]
        )
        self.assertEqual(summary["components_retained"], 27)
        self.assertAlmostEqual(summary["retained_variance_ratio"], 0.9)
        self.assertEqual(axes.shape, (30, 30))
        self.assertEqual(len(summary["spectrum"]), 30)

    def test_degenerate_covariance_does_not_invent_explained_variance(self):
        _, _, _, summary = decompose(
            100, np.zeros(3), np.zeros((3, 3)), ["a", "b", "c"]
        )
        self.assertEqual(summary["rank"], 0)
        self.assertIsNone(summary["retained_variance_ratio"])
        self.assertTrue(
            all(
                v["variance_ratio"] is None and not v["top_features"]
                for v in summary["spectrum"]
            )
        )

    def test_yearly_covariances_recover_different_dominant_inputs(self):
        _, _, _, old = decompose(
            100,
            np.zeros(3),
            100 * np.diag([8.0, 1.0, 1.0]),
            ["age", "mining", "combat"],
        )
        _, _, _, new = decompose(
            100,
            np.zeros(3),
            100 * np.diag([1.0, 8.0, 1.0]),
            ["age", "mining", "combat"],
        )
        self.assertEqual(old["spectrum"][0]["top_features"][0]["name"], "age")
        self.assertEqual(new["spectrum"][0]["top_features"][0]["name"], "mining")

    def test_opposite_yearly_effects_are_visible_despite_zero_global_average(self):
        baseline = {"2012": [100, 200], "2026": [100, 200]}
        groups = {
            "mining": {"2012": [80, 100], "2026": [20, 100]},
            "combat": {"2012": [20, 100], "2026": [80, 100]},
        }
        global_results = adjusted_association(groups, baseline)
        self.assertTrue(
            all(abs(v["year_adjusted_difference_pp"]) < 1e-10 for v in global_results)
        )
        yearly = yearly_associations(groups, baseline)
        effects = {
            year: next(v for v in values if v["value"] == "mining")
            for year, values in yearly.items()
        }
        self.assertAlmostEqual(effects["2012"]["difference_pp"], 60)
        self.assertAlmostEqual(effects["2026"]["difference_pp"], -60)
        self.assertEqual(effects["2026"]["other_n"], 100)
        self.assertEqual(effects["2026"]["other_returned"], 80)
        self.assertIsNotNone(effects["2012"]["interval"])

    def test_years_with_small_comparison_groups_are_left_unknown(self):
        effects = yearly_associations(
            {"A": {"2012": [3, 10], "2026": [20, 40]}},
            {"2012": [15, 100], "2026": [30, 100], "2025": [0, 0]},
        )
        self.assertEqual(effects["2012"], [])
        self.assertEqual(effects["2025"], [])
        self.assertEqual(len(effects["2026"]), 1)


if __name__ == "__main__":
    unittest.main()
