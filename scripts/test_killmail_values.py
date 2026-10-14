"""Unit and admin-control checks for in-place killmail valuation."""

import unittest
import os
import json
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts import calculate_killmail_values as values
from scripts.test_first_loss_admin import jobs


class PriceFormulaTests(unittest.TestCase):
    def test_rolling_price_trims_like_zkill(self):
        price, fallback = values.rolling_price(
            [Decimal(number) for number in range(1, 35)]
        )
        self.assertEqual(price, Decimal("17.50"))
        self.assertFalse(fallback)

        price, fallback = values.rolling_price(
            [Decimal(number) for number in range(1, 8)]
        )
        self.assertEqual(price, Decimal("3.00"))
        self.assertFalse(fallback)

    def test_exact_day_price_caps_a_higher_average(self):
        price, fallback = values.rolling_price(
            [Decimal("10"), Decimal("12")], Decimal("4.25")
        )
        self.assertEqual(price, Decimal("4.25"))
        self.assertFalse(fallback)

    def test_no_history_is_one_cent_and_reported(self):
        self.assertEqual(
            values.rolling_price([], None), (Decimal("0.01"), True)
        )

    def test_concrete_special_rules_precede_history(self):
        metadata = {
            1: (None, 91, "SKIN"),
            2: (29, 6, "Capsule"),
            3: (4513, None, "Mordunium"),
            42126: (30, 6, "Vanquisher"),
        }
        engine = values.Pricing(metadata, {}, {})
        self.assertEqual(
            engine.price(1, date(2026, 1, 1), date(2026, 1, 1))[0],
            Decimal("0.01"),
        )
        self.assertEqual(
            engine.price(2, date(2026, 1, 1), date(2026, 1, 1))[0],
            Decimal("10000"),
        )
        self.assertEqual(
            engine.price(3, date(2026, 1, 1), date(2026, 1, 1))[0],
            Decimal("200"),
        )
        self.assertEqual(
            engine.price(42126, date(2026, 1, 1), date(2026, 1, 1))[0],
            Decimal("650000000000"),
        )

    def test_price_stream_retains_history_and_materializes_only_needed_pairs(self):
        class Cursor:
            def __init__(self, rows):
                self.rows = iter(rows)

            def fetchone(self):
                return next(self.rows, None)

        rows = [
            (date(2026, 1, 1), {"34": Decimal("4")}),
            (date(2026, 1, 2), {"34": Decimal("8"), "35": Decimal("10")}),
            (date(2026, 1, 3), {"34": Decimal("12")}),
        ]
        cursor = Cursor(rows[1:])
        engine = values.Pricing({}, {}, {34: 3, 35: 1})
        pending, materialized, last_day = values.advance_prices(
            cursor,
            rows[0],
            date(2026, 1, 2),
            engine,
            {date(2026, 1, 2): [(date(2026, 1, 2), 34), (date(2026, 1, 2), 35)]},
        )
        self.assertEqual(pending[0], date(2026, 1, 3))
        self.assertEqual(last_day, date(2026, 1, 2))
        self.assertEqual(materialized, [
            (date(2026, 1, 2), date(2026, 1, 2), 34, Decimal("6"), False),
            (date(2026, 1, 2), date(2026, 1, 2), 35, Decimal("10"), False),
        ])

    def test_blueprint_material_efficiency_uses_ceiling_and_output_quantity(self):
        recipes = values.normalize_blueprints([{
            "activities": {"manufacturing": {
                "products": [{"typeID": 1, "quantity": 4}],
                "materials": [{"typeID": 2, "quantity": 11}],
            }}
        }])
        self.assertEqual(recipes, {1: (4, {2: 10})})

    def test_missing_hull_is_built_using_previous_day_material_prices(self):
        engine = values.Pricing({}, {1: (1, {2: 3})}, {2: 100})
        engine.add_day(date(2026, 1, 1), {"2": Decimal("10")})
        engine.add_day(date(2026, 1, 2), {"2": Decimal("30")})
        self.assertEqual(engine.price(1, date(2026, 1, 2), date(2026, 1, 2)), (Decimal("30"), False))

    def test_supercapital_uses_build_recipe_instead_of_exact_snapshot(self):
        engine = values.Pricing({1: (30, 6, "Titan")}, {1: (1, {2: 3})}, {1: 100, 2: 100})
        engine.add_day(date(2026, 1, 1), {"2": Decimal("10")})
        engine.add_day(date(2026, 1, 2), {"1": Decimal("1000"), "2": Decimal("30")})
        self.assertEqual(engine.price(1, date(2026, 1, 2), date(2026, 1, 2)), (Decimal("30"), False))

    def test_structure_module_uses_build_price_even_with_a_market_price(self):
        engine = values.Pricing({1: (1415, 66, "Structure module")}, {1: (1, {2: 3})}, {1: 100, 2: 100})
        engine.add_day(date(2026, 1, 1), {"2": Decimal("10")})
        engine.add_day(date(2026, 1, 2), {"1": Decimal("1000")})
        self.assertEqual(engine.price(1, date(2026, 1, 2), date(2026, 1, 2))[0], Decimal("30"))

    def test_sparse_history_does_not_reuse_an_observation_outside_calendar_limit(self):
        engine = values.Pricing({}, {}, {1: 2})
        engine.add_day(date(2026, 1, 1), {"1": Decimal("6000")})
        self.assertEqual(engine.price(1, date(2026, 1, 10), date(2026, 1, 10)), (Decimal(".01"), True))

    def test_zero_history_prices_are_skipped(self):
        engine = values.Pricing({}, {}, {1: 10})
        engine.add_day(date(2026, 1, 1), {"1": Decimal("10")})
        engine.add_day(date(2026, 1, 2), {"1": Decimal("0")})
        self.assertEqual(engine.price(1, date(2026, 1, 2), date(2026, 1, 2))[0], Decimal("10"))

    def test_recursive_construction_keeps_sub_cent_precision(self):
        engine = values.Pricing({}, {1: (3, {2: 1})}, {2: 10})
        engine.add_day(date(2026, 1, 1), {"2": Decimal("1")})
        price, _ = engine.price(1, date(2026, 1, 2), date(2026, 1, 2))
        self.assertEqual(values.cents(price * 3), 100)

    def test_cyclic_recipes_are_reported_without_recursing_forever(self):
        engine = values.Pricing({}, {1: (1, {2: 1}), 2: (1, {1: 1})}, {})
        self.assertEqual(engine.price(1, date(2026, 1, 2), date(2026, 1, 2)), (Decimal(".01"), True))

    def test_date_only_recipe_inputs_and_timestamp_fixed_price_boundaries(self):
        engine = values.Pricing({}, {}, {})
        day = date(2023, 12, 1)
        self.assertEqual(engine.price(3514, day, day)[0], Decimal("250000000000"))
        self.assertEqual(engine.price(3514, day, day, date_only=True)[0], Decimal("100000000000"))


class AdminJobTests(unittest.TestCase):
    def test_job_is_passive_and_has_no_user_range(self):
        with patch.object(jobs, "_read_config_file", return_value={"jobs": []}), patch.object(
            jobs, "_start_process"
        ) as launch:
            job = jobs.get_job("calculate_killmail_values")
            self.assertEqual(job["type"], "killmail_values")
            launch.assert_not_called()
            jobs.run_killmail_values()
            launch.assert_called_once_with(job, job["command"])

    def test_initial_sql_scan_is_announced_before_it_starts(self):
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (True,)
        with tempfile.TemporaryDirectory() as directory:
            progress = Path(directory) / "progress.json"

            def scan(*_):
                self.assertEqual(json.loads(progress.read_text())["phase"], "finding_missing_values")
                self.assertTrue(any("START pid=" in line for line in logs.output))
                self.assertTrue(any("phase=finding_missing_values" in line for line in logs.output))
                raise values.StopRequested()

            with patch("sys.argv", ["calculator", "--progress", str(progress)]), \
                 patch.object(values, "load_config", return_value={}), \
                 patch.object(values.psycopg2, "connect", return_value=conn), \
                 patch.object(values.signal, "signal"), \
                 patch.object(values, "target_months", side_effect=scan), \
                 self.assertLogs(values.LOG, level="INFO") as logs:
                self.assertEqual(values.main(), 0)
            self.assertEqual(json.loads(progress.read_text())["phase"], "paused")
            conn.rollback.assert_called_once()

    def test_rejected_duplicate_start_preserves_running_jobs_progress(self):
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (False,)
        with tempfile.TemporaryDirectory() as directory:
            progress = Path(directory) / "progress.json"
            original = '{"pid":123,"phase":"writing_values"}'
            progress.write_text(original)
            with patch("sys.argv", ["calculator", "--progress", str(progress)]), \
                 patch.object(values, "load_config", return_value={}), \
                 patch.object(values.psycopg2, "connect", return_value=conn), \
                 patch.object(values.signal, "signal"), self.assertLogs(values.LOG, level="INFO"):
                self.assertEqual(values.main(), 1)
            self.assertEqual(progress.read_text(), original)


class PriceDateRuleTests(unittest.TestCase):
    def test_exact_quote_is_used_even_if_the_older_average_is_lower(self):
        engine = values.Pricing({}, {}, {}, valuation_mode="loss-day")
        engine.add_day(date(2026, 1, 1), {"1": "10"})
        engine.add_day(date(2026, 1, 2), {"1": "30"})
        self.assertEqual(engine.price(1, date(2026, 1, 2), date(2026, 1, 2)), (Decimal("30"), False))

    def test_latest_earlier_quote_has_no_calendar_limit(self):
        engine = values.Pricing({}, {}, {}, valuation_mode="loss-day",
                                first_prices={1: (date(2003, 1, 1), Decimal("10"))})
        engine.add_day(date(2003, 1, 1), {"1": "10"})
        engine.add_day(date(2007, 1, 1), {"1": "20"})
        self.assertEqual(engine.price(1, date(2026, 1, 2), date(2026, 1, 2)), (Decimal("20"), False))

    def test_earlier_quote_wins_over_a_closer_later_one(self):
        engine = values.Pricing({}, {}, {}, valuation_mode="loss-day")
        engine.add_day(date(2003, 1, 1), {"1": "10"})
        engine.add_day(date(2026, 1, 3), {"1": "30"})
        self.assertEqual(engine.price(1, date(2026, 1, 2), date(2026, 1, 2)), (Decimal("10"), False))

    def test_first_later_quote_is_used_only_when_no_earlier_one_exists(self):
        engine = values.Pricing({}, {}, {}, valuation_mode="loss-day",
                                first_prices={1: (date(2007, 12, 17), Decimal("91500000.05"))})
        self.assertEqual(engine.price(1, date(2007, 12, 1), date(2007, 12, 1)),
                         (Decimal("91500000.05"), False))

    def test_zero_and_null_observations_do_not_replace_a_positive_price(self):
        engine = values.Pricing({}, {}, {}, valuation_mode="loss-day")
        engine.add_day(date(2026, 1, 1), {"1": "12"})
        engine.add_day(date(2026, 1, 2), {"1": "0"})
        engine.add_day(date(2026, 1, 3), {"1": None})
        self.assertEqual(engine.price(1, date(2026, 1, 3), date(2026, 1, 3)), (Decimal("12"), False))

    def test_missing_old_recipe_does_not_block_a_market_quote(self):
        from scripts.killmail_recipe_history import RecipeHistory
        engine = values.Pricing({1: (30, 6, "Titan")}, {}, {}, valuation_mode="loss-day",
                                recipe_history=RecipeHistory(runtime_dir=None),
                                first_prices={1: (date(2010, 1, 1), Decimal("1000000000"))})
        self.assertEqual(engine.price(1, date(2007, 12, 1), date(2007, 12, 1)),
                         (Decimal("1000000000"), False))

    def test_recursive_materials_use_the_same_date_selection_rule(self):
        engine = values.Pricing({1: (30, 6, "Titan")}, {1: (1, {2: 3})}, {}, valuation_mode="loss-day",
                                first_prices={2: (date(2010, 1, 1), Decimal("20"))})
        self.assertEqual(engine.price(1, date(2007, 12, 1), date(2007, 12, 1)), (Decimal("60"), False))


@unittest.skipUnless(os.environ.get("KILLMAIL_VALUES_TEST_DSN"), "Set an isolated killmail_values_test DSN")
class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.conn = values.psycopg2.connect(os.environ["KILLMAIL_VALUES_TEST_DSN"])
        if self.conn.info.dbname != "killmail_values_test":
            self.conn.close()
            raise RuntimeError("Use an isolated killmail_values_test database")
        with self.conn.cursor() as cur:
            cur.execute("""CREATE SCHEMA rawkm;
                CREATE TABLE rawkm.killmails(
                    killmail_id bigint,killmail_time timestamptz,victim_ship_type_id bigint,
                    estimated_value_cents bigint) PARTITION BY RANGE(killmail_time);
                CREATE TABLE rawkm.killmails_2026_01 PARTITION OF rawkm.killmails
                    FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
                CREATE TABLE rawkm.killmail_items(
                    killmail_id bigint,killmail_time timestamptz,item_index integer,item_type_id bigint,
                    flag integer,quantity_destroyed bigint,quantity_dropped bigint,singleton integer,
                    PRIMARY KEY(killmail_id,killmail_time,item_index)) PARTITION BY RANGE(killmail_time);
                CREATE TABLE rawkm.killmail_items_2026_01 PARTITION OF rawkm.killmail_items
                    FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
                CREATE TABLE public.sde_types(sde_key text PRIMARY KEY,data jsonb);""")

    def tearDown(self):
        self.conn.rollback()
        self.conn.close()

    def test_first_positive_quotes_can_follow_the_entire_target_kill_month(self):
        with self.conn.cursor() as cur:
            cur.execute("""CREATE SCHEMA market;
                CREATE TABLE market.zkill_daily_prices(price_date date PRIMARY KEY,prices jsonb);
                INSERT INTO market.zkill_daily_prices VALUES
                    ('2026-02-01','{"1":0,"2":-1}'),
                    ('2026-02-02','{"1":100,"2":null}'),
                    ('2026-02-03','{"1":50,"2":20}'),
                    ('2026-02-04','{"3":999}');
                INSERT INTO rawkm.killmails VALUES(1,'2026-01-02 12:00:00+00',1,NULL);""")
        first = values.load_first_prices(self.conn, date(2026, 2, 3))
        self.assertEqual(first, {1: (date(2026, 2, 2), Decimal("100")),
                                 2: (date(2026, 2, 3), Decimal("20"))})
        engine = values.Pricing({}, {}, {}, valuation_mode="loss-day", first_prices=first)
        day = date(2026, 1, 2)
        rows = values.boundary_prices(day, [(day, 1)], engine)
        self.assertEqual(values.update_month(self.conn, date(2026, 1, 1), rows, "loss-day")[0], 1)
        with self.conn.cursor() as cur:
            cur.execute("SELECT estimated_value_cents FROM rawkm.killmails")
            self.assertEqual(cur.fetchone()[0], 10000)

    def test_sql_preserves_values_and_rounds_only_the_final_total(self):
        with self.conn.cursor() as cur:
            cur.execute("""INSERT INTO rawkm.killmails VALUES
                (1,'2026-01-02 12:00:00+00',1,NULL),
                (2,'2026-01-02 12:00:00+00',1,777),
                (3,'2026-01-02 00:30:00+00',2,NULL),
                (4,'2026-01-02 01:00:00+00',2,NULL);
                INSERT INTO rawkm.killmail_items_2026_01 VALUES
                (1,'2026-01-02 12:00:00+00',0,3,5,3,0,0),
                (1,'2026-01-02 12:00:00+00',1,4,5,5,0,2),
                (1,'2026-01-02 12:00:00+00',2,5,179,1,0,0),
                (1,'2026-01-02 12:00:00+00',3,33329,89,1,0,0);""")
        day = date(2026, 1, 2)
        rows = [(day, day, 1, Decimal("1000"), False),
                (day, day, 3, Decimal(1) / 3, False),
                (day, day, 4, Decimal("999"), False),
                (day, day, 5, Decimal("999"), False),
                (day, day, 33329, Decimal("999"), False),
                (day, date(2026, 1, 1), 2, Decimal("2"), False),
                (day, day, 2, Decimal("3"), False)]
        needed = values.needed_prices(self.conn, date(2026, 1, 1))
        self.assertIn((day, 2), needed[date(2026, 1, 1)])
        self.assertFalse({4, 5, 33329} & {t for pairs in needed.values() for _day, t in pairs})
        updated, _ = values.update_month(self.conn, date(2026, 1, 1), rows)
        self.assertEqual(updated, 3)
        with self.conn.cursor() as cur:
            cur.execute("SELECT killmail_id,estimated_value_cents FROM rawkm.killmails ORDER BY killmail_id")
            self.assertEqual(cur.fetchall(), [(1,100107), (2,777), (3,200), (4,200)])
        self.assertEqual(values.target_months(self.conn), [])

    def test_old_blueprints_are_copies_only_when_nested(self):
        from scripts import sync_killmails

        with self.conn.cursor() as cur:
            cur.execute("""INSERT INTO public.sde_types VALUES
                ('10','{"name":{"en":"Example Blueprint"}}'),
                ('11','{"name":{"en":"Container"}}');""")
            original = {"item_type_id":10,"flag":5,"quantity_destroyed":1,"singleton":0}
            nested = {"item_type_id":11,"flag":5,"quantity_destroyed":1,"singleton":0,
                      "items":[dict(original)]}
            index = sync_killmails.insert_item_recursive(cur, 100, "2026-01-02", original, 0)
            sync_killmails.insert_item_recursive(cur, 100, "2026-01-02", nested, index)
            cur.execute("SELECT singleton FROM rawkm.killmail_items_2026_01 WHERE item_type_id=10 ORDER BY item_index")
            self.assertEqual(cur.fetchall(), [(0,), (2,)])

    def safety_fixture(self):
        """Keep complete raw rows so an accidental non-price write is visible."""
        with self.conn.cursor() as cur:
            cur.execute("""ALTER TABLE rawkm.killmails ADD COLUMN raw_payload jsonb;
                CREATE TABLE rawkm.killmails_2026_02 PARTITION OF rawkm.killmails
                    FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');
                CREATE TABLE rawkm.killmail_attackers(killmail_id bigint,raw_payload jsonb);
                INSERT INTO rawkm.killmails VALUES
                    (1,'2026-01-02 12:00:00+00',1,NULL,'{"victim":101,"system":30000142}'),
                    (2,'2026-01-02 12:00:00+00',1,777,'{"victim":102,"system":30000143}'),
                    (3,'2026-01-02 12:00:00+00',99,NULL,'{"victim":103,"system":30000144}'),
                    (1,'2026-02-02 12:00:00+00',1,NULL,'{"victim":104,"system":30000145}');
                INSERT INTO rawkm.killmail_items VALUES
                    (1,'2026-01-02 12:00:00+00',0,3,5,2,4,0),
                    (2,'2026-01-02 12:00:00+00',0,3,5,3,0,0);
                INSERT INTO rawkm.killmail_attackers VALUES
                    (1,'{"character":201,"final_blow":true}'),
                    (1,'{"character":202,"final_blow":false}'),
                    (2,'{"character":203,"final_blow":true}');""")
        day = date(2026, 1, 2)
        return [(day, day, 1, Decimal("100"), False),
                (day, day, 3, Decimal("2"), False)]

    def raw_snapshot(self, include_value=False):
        with self.conn.cursor() as cur:
            column = "to_jsonb(k)" if include_value else "to_jsonb(k)-'estimated_value_cents'"
            cur.execute(f"SELECT {column} FROM rawkm.killmails k ORDER BY killmail_time,killmail_id")
            kills = cur.fetchall()
            cur.execute("SELECT * FROM rawkm.killmail_items ORDER BY killmail_time,killmail_id,item_index")
            items = cur.fetchall()
            cur.execute("SELECT * FROM rawkm.killmail_attackers ORDER BY killmail_id,raw_payload::text")
            attackers = cur.fetchall()
        return kills, items, attackers

    def test_valuation_preserves_all_killmail_data_items_attackers_and_other_months(self):
        rows = self.safety_fixture()
        before = self.raw_snapshot()
        with self.conn.cursor() as cur:
            # The production schema setup is also safe to run repeatedly.
            cur.execute(values.SCHEMA.read_text())
            cur.execute(values.SCHEMA.read_text())
        self.assertEqual(values.update_month(self.conn, date(2026, 1, 1), rows, "loss-day")[0], 1)
        self.assertEqual(self.raw_snapshot(), before)
        with self.conn.cursor() as cur:
            cur.execute("SELECT estimated_value_cents FROM rawkm.killmails ORDER BY killmail_time,killmail_id")
            self.assertEqual(cur.fetchall(), [(11200,), (777,), (None,), (None,)])
            # Remove only session-local staging tables to repeat the pass in
            # this uncommitted fixture, as ON COMMIT DROP does in production.
            cur.execute("DROP TABLE pg_temp.valuation_stage,pg_temp.valuation_prices")
        after = self.raw_snapshot(include_value=True)
        self.assertEqual(values.update_month(self.conn, date(2026, 1, 1), rows, "loss-day")[0], 0)
        self.assertEqual(self.raw_snapshot(include_value=True), after)

    def test_rolling_back_a_valuation_preserves_every_raw_row_and_value(self):
        rows = self.safety_fixture()
        before = self.raw_snapshot(include_value=True)
        with self.conn.cursor() as cur:
            cur.execute("SAVEPOINT before_valuation")
        self.assertEqual(values.update_month(self.conn, date(2026, 1, 1), rows, "loss-day")[0], 1)
        with self.conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT before_valuation")
            cur.execute("SELECT to_regclass('pg_temp.valuation_stage'),to_regclass('pg_temp.valuation_prices')")
            self.assertEqual(cur.fetchone(), (None, None))
        self.assertEqual(self.raw_snapshot(include_value=True), before)

    def test_loss_day_recipe_transition_and_midnight_are_used_in_sql(self):
        import tempfile
        from pathlib import Path
        from scripts.killmail_recipe_history import RecipeHistory, record_snapshot

        def blueprints(hull_quantity, component_quantity, output):
            return [{"activities": {"manufacturing": {
                "products": [{"typeID": t, "quantity": n}],
                "materials": [{"typeID": m, "quantity": q}],
            }}} for t, n, m, q in [(1, 1, 2, hull_quantity), (2, output, 4, component_quantity)]]

        with tempfile.TemporaryDirectory() as directory:
            record_snapshot("test-before", "2026-01-01", blueprints(10, 10, 1), "test", directory)
            record_snapshot("test-after", "2026-01-02", blueprints(20, 5, 2), "test", directory)
            history = RecipeHistory(runtime_dir=directory)
            engine = values.Pricing({1: (30, 6, "Titan")}, {}, {4: 100},
                                    valuation_mode="loss-day", recipe_history=history)
            engine.add_day(date(2026, 1, 1), {"4": "90"})
            engine.add_day(date(2026, 1, 2), {"4": "30"})
            with self.conn.cursor() as cur:
                cur.execute("""INSERT INTO rawkm.killmails VALUES
                    (1,'2026-01-01 23:30:00+00',1,NULL),
                    (2,'2026-01-02 00:30:00+00',1,NULL),
                    (3,'2026-01-03 12:00:00+00',1,NULL);""")
            needed = values.needed_prices(self.conn, date(2026, 1, 1), "loss-day")
            self.assertIn((date(2026, 1, 2), 1), needed[date(2026, 1, 2)])
            rows = [row for day, pairs in needed.items() if day <= date(2026, 1, 2)
                    for row in values.boundary_prices(day, pairs, engine)]
            updated, _ = values.update_month(self.conn, date(2026, 1, 1), rows, "loss-day")
            self.assertEqual(updated, 2)
            with self.conn.cursor() as cur:
                cur.execute("SELECT killmail_id,estimated_value_cents FROM rawkm.killmails ORDER BY killmail_id")
                self.assertEqual(cur.fetchall(), [(1, 729000), (2, 135000), (3, None)])
            self.assertEqual(values.target_months(self.conn, "loss-day"),
                             [(date(2026, 1, 1), date(2026, 1, 3))])

    def test_unavailable_recipe_does_not_store_a_fictional_one_cent_price(self):
        day = date(2026, 1, 2)
        with self.conn.cursor() as cur:
            cur.execute("""INSERT INTO rawkm.killmails VALUES
                (1,'2026-01-02 12:00:00+00',1,NULL),
                (2,'2026-01-02 12:00:00+00',2,NULL);
                INSERT INTO rawkm.killmail_items_2026_01 VALUES
                (2,'2026-01-02 12:00:00+00',0,3,5,1,0,0);""")
        rows = [(day, day, 1, None, True), (day, day, 2, Decimal("100"), False),
                (day, day, 3, None, True)]
        self.assertEqual(values.update_month(self.conn, date(2026, 1, 1), rows, "loss-day")[0], 0)
        with self.conn.cursor() as cur:
            cur.execute("SELECT estimated_value_cents FROM rawkm.killmails")
            self.assertEqual(cur.fetchall(), [(None,), (None,)])

    def test_historical_hel_values_are_written_to_the_killmail_at_the_cent(self):
        from scripts.test_killmail_recipe_history import historical_fixture, fixture_pricing

        fixture = historical_fixture()
        for case in fixture["cases"]:
            with self.subTest(killmail_id=case["killmail_id"]):
                stamp = values.datetime.fromisoformat(case["killmail_time"].replace("Z", "+00:00"))
                month = stamp.date().replace(day=1)
                with self.conn.cursor() as cur:
                    for prefix in ("killmails", "killmail_items"):
                        cur.execute(values.sql.SQL(
                            "CREATE TABLE rawkm.{} PARTITION OF rawkm.{} FOR VALUES FROM ({}) TO ({})"
                        ).format(values.sql.Identifier(values.relation_name(prefix, month)),
                                 values.sql.Identifier(prefix), values.sql.Literal(month),
                                 values.sql.Literal(values.next_month(month))))
                    cur.execute("INSERT INTO rawkm.killmails VALUES (%s,%s,%s,NULL)",
                                (case["killmail_id"], stamp, case["ship_type_id"]))
                    for index, item in enumerate(case["items"]):
                        cur.execute("INSERT INTO rawkm.killmail_items VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                                    (case["killmail_id"], stamp, index, item["item_type_id"],
                                     item.get("flag"), item.get("quantity_destroyed", 0),
                                     item.get("quantity_dropped", 0), item.get("singleton", 0)))
                engine = fixture_pricing(case, fixture["recipe_version"], fixture["cache_rules_at"])
                needed = values.needed_prices(self.conn, month)
                price_rows = [row for day, pairs in needed.items()
                              for row in values.boundary_prices(day, pairs, engine)]
                updated, _fallbacks = values.update_month(self.conn, month, price_rows)
                self.assertEqual(updated, 1)
                with self.conn.cursor() as cur:
                    cur.execute("SELECT estimated_value_cents FROM rawkm.killmails WHERE killmail_id=%s",
                                (case["killmail_id"],))
                    self.assertEqual(cur.fetchone()[0], values.cents(Decimal(case["expected"])))
                    cur.execute("DROP TABLE valuation_stage,valuation_prices")


if __name__ == "__main__":
    unittest.main()
