"""Scientific diagnostics and bounded automatic model selection, offline."""

import unittest
import numpy as np
from scripts.first_loss_study import clustering as c


class ClusteringTests(unittest.TestCase):
    def test_modes_and_invalid_input(self):
        self.assertEqual(c.normalize_clusters(" AUTO "), "auto")
        self.assertEqual(c.normalize_clusters("8"), 8)
        for value in (0, 31, "auto; command", "2.5", None):
            with self.assertRaises(ValueError):
                c.normalize_clusters(value)

    def test_silhouette_matches_simple_distances_and_handles_singletons(self):
        x = np.array([[0.0], [1.0], [10.0], [11.0]])
        value = c.silhouette(c.pairwise_distances(x), np.array([0, 0, 1, 1]))
        self.assertAlmostEqual(
            value, np.mean([1 - 1 / 10.5, 1 - 1 / 9.5, 1 - 1 / 9.5, 1 - 1 / 10.5])
        )
        self.assertIsNone(c.silhouette(c.pairwise_distances(x), np.zeros(4)))
        self.assertIsNone(c.silhouette(c.pairwise_distances(x), np.arange(4)))
        self.assertTrue(
            np.isfinite(c.silhouette(c.pairwise_distances(x), np.array([0, 0, 0, 1])))
        )

    def test_adjusted_rand_is_invariant_to_labels_and_handles_large_counts(self):
        self.assertEqual(
            c.adjusted_rand(np.array([0, 0, 1, 1]), np.array([1, 1, 0, 0])), 1.0
        )
        self.assertAlmostEqual(
            c.adjusted_rand(np.array([0, 0, 1, 1]), np.array([0, 1, 0, 1])), -0.5
        )
        self.assertEqual(c.ari_from_counts(np.diag([10_000_000, 10_000_000])), 1.0)
        self.assertEqual(c.adjusted_rand(np.zeros(4), np.zeros(4)), 1.0)

    def test_selection_prefers_stability_then_fewer_nearly_equal_groups(self):
        def row(k, score, stability):
            return {
                "k": k,
                "valid": True,
                "silhouette_mean": score,
                "stability_ari": stability,
                "min_training_fraction": 0.1,
            }

        chosen, warnings = c.choose_candidate(
            [row(2, 0.49, 0.9), row(3, 0.5, 0.95), row(8, 0.7, 0.2)]
        )
        self.assertEqual(chosen["k"], 2)
        self.assertEqual(warnings, [])
        chosen, warnings = c.choose_candidate([row(2, 0.1, 0.4), row(3, 0.5, 0.5)])
        self.assertEqual(chosen["k"], 3)
        self.assertTrue(warnings)

    def test_auto_recovers_three_separated_profiles_and_reports_every_candidate(self):
        rng = np.random.default_rng(123)
        points = np.concatenate(
            [
                rng.normal(center, 0.12, size=(300, 2))
                for center in [(-6.0, 0.0), (0.0, 6.0), (6.0, 0.0)]
            ]
        )
        events = []
        fits, metadata, diagnostic, confirmation = c.select_profiles(
            points, "auto", lambda phase, **kw: events.append((phase, kw)), lambda: None
        )
        self.assertEqual(metadata["selected_k"], 3)
        self.assertEqual(
            [row["k"] for row in metadata["candidates"]], list(range(2, 31))
        )
        self.assertEqual(len(fits), 3)
        self.assertEqual(len(diagnostic), 150)
        self.assertEqual(len(confirmation), 150)
        self.assertAlmostEqual(
            next(row for row in metadata["candidates"] if row["k"] == 3)[
                "stability_ari"
            ],
            1.0,
        )
        self.assertEqual(events[-1][0], "profiles_selected")
        repeated = c.select_profiles(
            points, "auto", lambda *a, **kw: None, lambda: None
        )[1]
        self.assertEqual(metadata, repeated)

    def test_no_variation_retains_one_group_and_does_not_invent_scores(self):
        fits, metadata, _, _ = c.select_profiles(
            np.zeros((50, 2)), "auto", lambda *a, **kw: None, lambda: None
        )
        self.assertEqual(metadata["selected_k"], 1)
        self.assertEqual(metadata["candidates"], [])
        self.assertTrue(metadata["warnings"])
        self.assertEqual(len(fits), 1)
        with self.assertRaises(ValueError):
            c.select_profiles(np.zeros((50, 2)), 8, lambda *a, **kw: None, lambda: None)

    def test_stop_interrupts_selection_before_more_candidates_are_fitted(self):
        def stop():
            raise InterruptedError("requested")

        with self.assertRaises(InterruptedError):
            c.select_profiles(
                np.random.default_rng(1).normal(size=(60, 2)),
                "auto",
                lambda *a, **kw: None,
                stop,
            )


if __name__ == "__main__":
    unittest.main()
