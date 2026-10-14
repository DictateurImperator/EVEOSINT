"""Historical CCP recipes, version selection and recorded Hel regressions."""

import argparse
import gzip
import json
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from scripts import calculate_killmail_values as values
from scripts.killmail_recipe_history import CATALOG, MANIFEST, RecipeHistory, record_snapshot


def historical_fixture():
    return json.loads(gzip.decompress(
        Path(__file__).with_name("testdata").joinpath("historical_hel_values.json.gz").read_bytes()
    ))


def fixture_pricing(case, recipe_version, cache_rules_at, history=None):
    observations = {(row["type_id"], row["day"], row["earliest"]): row["observations"]
                    for row in case["observations"]}
    counts = {int(t): count for t, count in case["history_counts"].items()}
    metadata = {int(t): row for t, row in case["metadata"].items()}

    def lookup(type_id, day, earliest):
        if not counts.get(type_id):
            return []
        rows = observations[(type_id, day.isoformat(), earliest.isoformat())]
        return [(date.fromisoformat(d), Decimal(price)) for d, price in rows]

    return values.Pricing(
        metadata, values.load_blueprints(None, recipe_version, history), counts, lookup,
        rules_at=values.valuation_timestamp(cache_rules_at),
    )


class RecipeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.history = RecipeHistory(runtime_dir=None)

    def test_every_bundled_snapshot_reconstructs_its_verified_recipe_set(self):
        self.assertGreaterEqual(len(self.history.versions), 157)
        for version, metadata in self.history.versions.items():
            with self.subTest(version=version):
                self.assertEqual(len(self.history.recipes(version)), metadata["recipe_count"])

    def test_exact_version_alias_does_not_guess_through_snapshot_gaps(self):
        self.assertEqual(self.history.resolve("2022-05-10"), "20220510")
        self.assertEqual(self.history.resolve("build-3579973"), "build-3579973")
        for missing in ("2003-01-01", "2022-05-09", "2026-10-13", "../data", "current"):
            with self.subTest(version=missing), self.assertRaises(ValueError):
                self.history.resolve(missing)

    def test_loss_date_uses_past_recipes_and_confirmed_patch_dates(self):
        for day, version in [
            ("2014-07-21", None), ("2014-07-22", "fuzzwork-crius-1.0-100038"),
            ("2014-09-29", "fuzzwork-hyperion-1.0-101505"),
            ("2014-09-30", "fuzzwork-oceanus-1.0-105658"),
            ("2015-12-07", "fuzzwork-parallax-1-115480"),
            ("2015-12-08", "fuzzwork-frostline-1.0-116241"),
            ("2016-03-07", "fuzzwork-yc118-2-116998"),
            ("2016-03-08", "fuzzwork-yc118-3-117575"),
            ("2016-10-11", "20161011"),
            ("2021-04-26", "20210413"), ("2021-04-27", "20210427"),
            ("2022-05-09", "20220405"), ("2022-05-10", "20220510"),
            ("2023-06-12", "20230315"), ("2023-06-13", "20230619"),
            ("2024-09-17", "20240909"), ("2024-09-18", "20240919"),
        ]:
            with self.subTest(day=day):
                self.assertEqual(self.history.version_on(date.fromisoformat(day)), version)

    def test_imported_sde_extends_history_without_replacing_old_dates(self):
        rows = [{"activities": {"manufacturing": {
            "products": [{"typeID": 1, "quantity": 3}],
            "materials": [{"typeID": 2, "quantity": 11}],
        }}}]
        with tempfile.TemporaryDirectory() as directory:
            record_snapshot("build-test-1", "2026-11-01T11:00:00Z", rows, "https://example.test/ccp-sde", directory)
            record_snapshot("build-test-1", "2026-11-01T11:00:00Z", rows, "https://example.test/ccp-sde", directory)
            record_snapshot("build-test-2", "2026-12-01T11:00:00Z", rows, "https://example.test/ccp-sde", directory)
            history = RecipeHistory(runtime_dir=directory)
            self.assertEqual(history.version_on(date(2026, 11, 1)), "build-test-1")
            self.assertEqual(history.version_on(date(2026, 12, 1)), "build-test-2")
            self.assertEqual(history.version_on(date(2022, 5, 10)), "20220510")
            self.assertEqual(values.load_blueprints(None, "build-test-1", history), {1: (3, {2: 10})})
            self.assertEqual(len(list(Path(directory).glob("*.json.gz"))), 1)

    def test_loss_day_keeps_component_recipes_and_prices_on_the_loss_day(self):
        engine = values.Pricing({1: (30, 6, "Titan")}, {1: (1, {2: 3}), 2: (2, {3: 4})},
                                {3: 100}, valuation_mode="loss-day")
        engine.add_day(date(2026, 1, 1), {"3": "90"})
        engine.add_day(date(2026, 1, 2), {"3": "30"})
        self.assertEqual(engine.price(1, date(2026, 1, 2), date(2026, 1, 1)), (Decimal("180"), False))

    def test_recipe_change_clears_market_and_build_quotes(self):
        engine = values.Pricing({}, {}, {}, valuation_mode="loss-day", recipe_history=self.history)
        engine.select_recipe_day(date(2022, 5, 9))
        engine.quotes.market_put(1, date(2022, 5, 9), (Decimal("10"), False))
        engine.quotes.build_put(1, date(2022, 5, 9), (Decimal("10"), False))
        engine.select_recipe_day(date(2022, 5, 10))
        self.assertEqual(engine.recipe_version, "20220510")
        self.assertFalse(engine.quotes.market)
        self.assertFalse(engine.quotes.built)
        self.assertEqual(engine.recipes[22852][1][57486], 360)

    def test_uncovered_construction_is_left_empty_but_known_market_prices_work(self):
        engine = values.Pricing({1: (30, 6, "Titan")}, {}, {2: 100},
                                valuation_mode="loss-day", recipe_history=self.history)
        day = date(2009, 1, 2)
        engine.add_day(day, {"2": "12"})
        rows = values.boundary_prices(day, [(day, 1), (day, 2)], engine)
        self.assertEqual(rows, [(day, day, 1, None, True), (day, day, 2, Decimal("12"), False)])

    def test_loss_day_refresh_does_not_preserve_a_quote_from_partial_prices(self):
        engine = values.Pricing({}, {}, {1: 100}, valuation_mode="loss-day")
        day = date(2026, 1, 1)
        engine.add_day(day, {"1": "10"})
        self.assertEqual(engine.price(1, day, day)[0], Decimal("10"))
        engine.add_day(day, {"1": "30"})
        self.assertEqual(engine.price(1, day, day)[0], Decimal("30"))

    def test_changed_output_and_complete_component_chain_are_historical(self):
        before = values.load_blueprints(None, "2022-04-05", self.history)
        after = values.load_blueprints(None, "2022-05-10", self.history)
        self.assertEqual(before[57486][0], 1)  # Life Support Backup Unit
        self.assertEqual(after[57486][0], 3)
        self.assertEqual(before[22852][1][57486], 2250)
        self.assertEqual(after[22852][1][57486], 360)
        self.assertEqual(before[57487][1][57479], 225)
        self.assertEqual(after[57487][1][57479], 63)

    def test_later_titan_recipe_and_removed_materials_are_applied(self):
        before = values.load_blueprints(None, "2024-09-09", self.history)
        after = values.load_blueprints(None, "2024-09-19", self.history)
        self.assertEqual(before[671][1][57486], 720)
        self.assertEqual(after[671][1][57486], 1440)
        # 2023 changes replace the planetary inputs, rather than merge them.
        old = values.load_blueprints(None, "2023-01-17", self.history)
        new = values.load_blueprints(None, "2023-06-19", self.history)
        self.assertIn(3645, old[57486][1])  # Water
        self.assertNotIn(3645, new[57486][1])

    def test_archived_loading_never_connects_to_current_sde(self):
        class Connection:
            def cursor(self):
                raise AssertionError("Historical recipes must not query current SDE")
        self.assertIn(22852, values.load_blueprints(Connection(), "20220510", self.history))

    def test_corrupt_catalog_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "catalog.gz"
            file.write_bytes(CATALOG.read_bytes() + b"corruption")
            with self.assertRaisesRegex(ValueError, "checksum"):
                RecipeHistory(file, MANIFEST)

    def test_cache_era_is_explicit_and_timezone_aware(self):
        self.assertEqual(values.valuation_timestamp("2022-08-01"), datetime(2022, 8, 1, tzinfo=UTC))
        self.assertEqual(values.valuation_timestamp("2022-08-01T02:00:00+02:00"),
                         datetime(2022, 8, 1, tzinfo=UTC))
        for bad in ("invalid", "2022-08-01T02:00:00", "2022-02-30"):
            with self.subTest(value=bad), self.assertRaises(argparse.ArgumentTypeError):
                values.valuation_timestamp(bad)

    def test_list_and_bad_version_exit_before_connecting_or_writing_schema(self):
        with patch.object(values.psycopg2, "connect") as connect:
            with patch("sys.argv", ["calculator", "--list-recipe-versions"]), patch("builtins.print"):
                self.assertEqual(values.main(), 0)
            with patch("sys.argv", ["calculator", "--recipe-version", "2003-01-01"]), patch("sys.stderr"):
                with self.assertRaises(SystemExit) as error:
                    values.main()
                self.assertEqual(error.exception.code, 2)
            connect.assert_not_called()


class HistoricalHelTests(unittest.TestCase):
    def test_two_recorded_hel_totals_are_reproduced_to_the_cent(self):
        fixture = historical_fixture()
        history = RecipeHistory()
        for case in fixture["cases"]:
            with self.subTest(killmail_id=case["killmail_id"]):
                def calculate(version):
                    engine = fixture_pricing(case, version, fixture["cache_rules_at"], history)
                    stamp = datetime.fromisoformat(case["killmail_time"].replace("Z", "+00:00"))
                    day = stamp.date()
                    market_day = (stamp - timedelta(seconds=3601)).date()
                    total, _ = engine.price(case["ship_type_id"], day, market_day)
                    for item in case["items"]:
                        type_id = item["item_type_id"]
                        special = (item.get("singleton") == 2 or item.get("flag") == 179 or
                                   (type_id == 33329 and item.get("flag") == 89))
                        unit = Decimal(".01") if special else engine.price(type_id, day, market_day)[0]
                        total += unit * (item.get("quantity_destroyed", 0) + item.get("quantity_dropped", 0))
                    return values.cents(total)

                expected = values.cents(Decimal(case["expected"]))
                self.assertEqual(calculate(fixture["recipe_version"]), expected)
                self.assertNotEqual(calculate("2025-07-07"), expected)


if __name__ == "__main__":
    unittest.main()
