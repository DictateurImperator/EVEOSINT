"""Outcome separation, adjusted associations, diagnostics and birthday repair."""

from datetime import datetime, timedelta, timezone
import unittest
import copy
from unittest.mock import patch
import numpy as np
from scripts.first_loss_study.quality import clean_features
from scripts.first_loss_study.return_model import (
    ReturnStudy,
    is_test,
    sigmoid,
    refresh_global_importance,
)


def population(size=2500):
    rng = np.random.default_rng(41)
    rows = []
    for cid in range(size):
        age = 2 if rng.random() < 0.5 else 400
        mining = rng.random() < 0.5
        npc = rng.random() < 0.25
        probability = float(sigmoid(0.7 - 1.2 * (age > 7) - 1.0 * mining + 0.4 * npc))
        returned = bool(rng.random() < probability)
        f = dict(
            age_days=age,
            ship_role="mining" if mining else "combat",
            space="HS",
            corp_kind="player",
            opponent="NPC/object" if npc else "players",
            prior_attacks=int(rng.integers(0, 20)),
            players_attacking=0 if npc else 1,
            npc_attackers=1 if npc else 0,
        )
        rows.append(
            dict(
                character_id=cid,
                cohort=2020 + cid % 2,
                features=f,
                outcomes={
                    **{
                        str(h): dict(followup_complete=True, returned=returned)
                        for h in (7, 30, 90)
                    },
                    "initial_pvp": not npc,
                },
            )
        )
    return rows


def fit(rows):
    study = ReturnStudy()
    study.normalize(rows)
    study.prepare()
    for i in range(16):
        study.fit_batch(rows)
        if study.step():
            break
    study.score_batch(rows)
    return study


class ReturnModelTests(unittest.TestCase):
    def test_whole_year_permutations_detect_age_even_in_constant_age_batches(self):
        rows = sorted(population(2400), key=lambda r: r["features"]["age_days"])
        for index, row in enumerate(rows):
            row["character_id"] = index
        study = ReturnStudy()
        study.normalize(rows)
        study.prepare()
        batches = [rows[i : i + 100] for i in range(0, len(rows), 100)]
        for i in range(16):
            for batch in batches:
                study.fit_batch(batch)
            if study.step():
                break
        for batch in batches:
            study.score_batch(batch)
        archive, report = study.summarize()
        age = next(
            v
            for v in report["scopes"]["all"]["90"]["importance"]
            if v["factor"] == "age"
        )
        self.assertGreater(age["importance_percent"], 10)
        self.assertGreater(age["interval"][0], 0)
        refreshed = copy.deepcopy(report)
        with patch(
            "scripts.first_loss_study.database.stream", return_value=iter(batches)
        ):
            refresh_global_importance(None, 1, archive, refreshed)
        for scope in ("all", "pvp", "npc"):
            np.testing.assert_allclose(
                [v["delta_brier"] for v in report["scopes"][scope]["90"]["importance"]],
                [
                    v["delta_brier"]
                    for v in refreshed["scopes"][scope]["90"]["importance"]
                ],
                atol=1e-12,
            )

    def test_permutation_phase_obeys_stop_requests(self):
        study = fit(population(500))
        with self.assertRaisesRegex(RuntimeError, "stop requested"):
            study.summarize(
                check_stop=lambda: (_ for _ in ()).throw(RuntimeError("stop requested"))
            )

    def test_epoch_birthdays_are_unknown_with_original_age_preserved(self):
        loss = datetime(2007, 12, 1, tzinfo=timezone.utc)
        age = (loss - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds() / 86400
        old = dict(age_days=age, observed_since_birth=True, ship_role="combat")
        cleaned, changed = clean_features(old, loss)
        self.assertTrue(changed)
        self.assertIsNone(cleaned["age_days"])
        self.assertEqual(cleaned["age_days_raw"], age)
        self.assertFalse(cleaned["observed_since_birth"])
        self.assertEqual(old["age_days"], age)
        self.assertEqual(clean_features(cleaned, loss), (cleaned, False))
        self.assertEqual(
            clean_features(dict(age_days=30), loss), (dict(age_days=30), False)
        )

    def test_returns_and_ship_age_effects_are_evaluated_on_every_heldout_character(
        self,
    ):
        rows = population()
        study = fit(rows)
        archive, report = study.summarize()
        self.assertTrue(report["converged"])
        model = report["scopes"]["all"]["90"]
        self.assertEqual(model["test_n"], sum(is_test(r["character_id"]) for r in rows))
        self.assertEqual(model["training_n"] + model["test_n"], len(rows))
        self.assertLess(model["brier"], model["baseline_brier"])
        self.assertGreater(model["auc"], 0.65)
        age = next(
            v
            for v in model["effects"]
            if v["factor"] == "age" and v["profile"] == "1-5 years"
        )
        ship = next(
            v
            for v in model["effects"]
            if v["factor"] == "ship_role" and v["profile"] == "mining"
        )
        self.assertLess(age["effect_pp"], -10)
        self.assertLess(ship["effect_pp"], -10)
        self.assertLess(age["interval_pp"][1], 0)
        self.assertAlmostEqual(
            sum(v["importance_percent"] for v in model["importance"]), 100
        )
        self.assertEqual(
            sum(v["test_n"] for v in model["years"].values()), model["test_n"]
        )
        self.assertIn("robust_covariance", archive["models"]["all:90"])
        pvp = report["scopes"]["pvp"]["90"]
        self.assertEqual(
            pvp["training_n"] + pvp["test_n"],
            sum(r["outcomes"]["initial_pvp"] for r in rows),
        )
        npc = report["scopes"]["npc"]["90"]
        self.assertFalse(
            any(
                v["factor"] in ("players_attacking", "npc_attackers")
                for v in npc["effects"]
            )
        )

    def test_heldout_outcomes_do_not_enter_fitting_or_preprocessing(self):
        rows = population(500)
        left = fit(rows)
        altered = [
            (
                {
                    **r,
                    "outcomes": {
                        **r["outcomes"],
                        **{
                            str(h): dict(
                                followup_complete=True,
                                returned=not r["outcomes"][str(h)]["returned"],
                            )
                            for h in (7, 30, 90)
                        },
                    },
                }
                if is_test(r["character_id"])
                else r
            )
            for r in rows
        ]
        right = fit(altered)
        for key in left.models:
            np.testing.assert_allclose(
                left.models[key]["beta"], right.models[key]["beta"], atol=1e-12
            )

    def test_missing_followup_and_one_class_do_not_invent_effects(self):
        rows = population(500)
        for r in rows:
            r["outcomes"]["7"]["followup_complete"] = False
            r["outcomes"]["30"]["returned"] = True
        report = fit(rows).summarize()[1]
        self.assertIn("unavailable", report["scopes"]["all"]["7"])
        self.assertIn("unavailable", report["scopes"]["all"]["30"])


if __name__ == "__main__":
    unittest.main()
