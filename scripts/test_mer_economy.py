"""Economic MER tests. PostgreSQL tests use only an explicitly selected local lab."""

import base64
import csv
import io
import json
import os
import struct
import tempfile
import unittest
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from scripts import import_mer_economy as economy

MONTH = date(2026, 8, 1)


def archive(path, members):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as output:
        for name, rows in members.items():
            if isinstance(rows, str):
                output.writestr(name, rows)
            else:
                text = io.StringIO()
                writer = csv.writer(text)
                writer.writerows(rows)
                output.writestr(name, text.getvalue())
    return path


class ParserTests(unittest.TestCase):
    def test_historical_filename_formats(self):
        for name, month in {"EVEOnline_MER_202608.zip": MONTH,
                            "EVEOnline_MER_Aug17.zip": date(2017, 8, 1),
                            "January_2022_MER.zip": date(2022, 1, 1),
                            "EVEOnline_MER_Dec2016_v1.1.zip": date(2016, 12, 1),
                            "EVEOnline_MER_Sept2021.zip": date(2021, 9, 1)}.items():
            self.assertEqual(economy.report_month(Path(name)), month)

    def test_old_and_new_regional_headers_keep_exact_precision(self):
        for row in ({"regionID": "10000001", "regionName": "Derelik", "total.production": "12345678901234567890.123456", "mining.value": "0"},
                    {"region_id": "10000001", "region_name": "Derelik", "total_production": "12345678901234567890.123456", "mining_value": "0"}):
            facts, extra = economy.normalized_row("regional", row, MONTH, {})
            self.assertIsNone(extra)
            self.assertEqual(facts[0][6], Decimal("12345678901234567890.123456"))
            self.assertEqual(facts[1][6], 0)
            self.assertEqual(facts[0][-1], ("region", "10000001", "Derelik", 10000001))

    def test_missing_metric_is_not_zero(self):
        facts, extra = economy.normalized_row("money_supply", {"date": "2026-08-01", "character": "", "corporation": "NA", "total": "0"}, MONTH, {})
        self.assertEqual([f[4] for f in facts], ["total_isk"])
        self.assertEqual(facts[0][6], 0)
        self.assertIsNone(extra)

    def test_region_name_resolution_and_aggregate_scope(self):
        for label, scope in [("Derelik", "region"), ("Wormhole", "space_group"), ("Future Region", "region_name")]:
            facts, _ = economy.normalized_row("regional", {"region_name": label, "mined_value": "1"}, MONTH, {"derelik": 10000001})
            self.assertEqual(facts[0][-1][0], scope)

    def test_wh_trade_has_class_and_category_not_a_system(self):
        facts, _ = economy.normalized_row("wormhole_trade", {"wormhole_class": "Class 5", "item_category": "Gas", "import_or_export": "Exports", "total": "7"}, MONTH, {})
        self.assertEqual(facts[0][-1], ("wormhole_class", "5", "Class 5", None))
        self.assertEqual(facts[0][5], {"item_category": "Gas", "direction": "Exports"})

    def test_global_stock_and_velocity_daily(self):
        facts, _ = economy.normalized_row("money_supply", {"history_date": "2026-08-03", "total_isk": "10", "isk_velocity": "0.1"}, MONTH, {})
        self.assertEqual(facts[0][1:3], (date(2026, 8, 3), "day"))
        self.assertEqual(facts[1][7], "ratio")

    def test_snapshot_and_daily_flows_are_separate_and_signed(self):
        facts, _ = economy.normalized_row("isk_flows", {"keyText": "Insurance", "Faucet": "100", "Sink": "-20", "category": "Insurance"}, MONTH, {})
        self.assertEqual(facts[0][2], "month")
        self.assertEqual(facts[1][6], -20)
        daily, _ = economy.normalized_row("isk_flows", {"history_date": "2026-08-01", "entry_name": "Insurance", "entry_id": "-1.0", "entry_sink_value": "-2", "entry_faucet_value": "10"}, MONTH, {})
        self.assertEqual(daily[0][2], "day")
        self.assertEqual(daily[0][5]["entry_id"], "-1")

    def test_moon_units_and_old_metenox_spelling(self):
        row = {"region_name": "Derelik", "source": "metanox_mining", "quantity": "10"}
        regional, _ = economy.normalized_row("moon_region", row, MONTH, {})
        self.assertEqual(regional[0][7], "ISK")
        self.assertEqual(regional[0][5]["source"], "metenox_mining")
        history, _ = economy.normalized_row("moon_materials", {"history_date": "2026-08-02", "source": "refinery_mining", "moon_class": "r64", "quantity": "10"}, MONTH, {})
        self.assertEqual(history[0][7], "items")

    def test_future_datasets_and_columns_are_preserved(self):
        self.assertEqual(economy.normalized_row("unknown", {"value": "unfamiliar", "future": "data"}, MONTH, {})[1], {"value": "unfamiliar", "future": "data"})
        self.assertEqual(economy.normalized_row("money_supply", {"total": "5", "new_metric": "7"}, MONTH, {})[1], {"new_metric": "7"})

    def test_bad_numbers_are_not_silently_dropped(self):
        for value in ("oops", "Infinity", "-Infinity"):
            with self.assertRaises(ValueError):
                economy.number(value)

    def test_plotly_levels_list_and_encoded_arrays(self):
        for ys in ([1, 2], {"dtype": "f8", "bdata": base64.b64encode(struct.pack("<dd", 1, 2)).decode()}):
            data = [{"name": "Consumer Price Index", "x": ["2026-07-01", "2026-08-01"], "y": ys}]
            facts = list(economy.chart_facts('Plotly.newPlot("plot", ' + json.dumps(data) + ', {});', "20_economy_indices.html"))
            self.assertEqual([f[6] for f in facts], [1, 2])

    def test_chart_annotation_markers_are_not_economic_series(self):
        data = [{"x": ["2026-08-01"], "y": [100]},
                {"name": "Mineral Price Index", "x": ["2026-08-01"], "y": [40]}]
        facts = list(economy.chart_facts('Plotly.newPlot("plot", ' + json.dumps(data) + ', {});', "20_economy_indices.html"))
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0][5]["index"], "Mineral Price Index")

    def test_kill_csv_never_opened_and_dry_run_never_connects(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = archive(Path(tmp) / "EVEOnline_MER_202608.zip", {
                "data/kill_dump.csv": "not even valid CSV",
                "data/money_supply.csv": [["history_date", "total_isk"], ["2026-08-01", "10"]],
                "data/new_economics.csv": [["new"], ["hello"]],
            })
            real_open = zipfile.ZipFile.open

            def checked_open(instance, name, *args, **kwargs):
                self.assertNotIn("kill_dump", str(name))
                return real_open(instance, name, *args, **kwargs)

            with patch.object(zipfile.ZipFile, "open", checked_open), patch.object(economy, "connect") as connect:
                self.assertEqual(economy.main(["--archive", str(path), "--dry-run"]), 0)
                connect.assert_not_called()


@unittest.skipUnless(os.environ.get("EVEOSINT_FORENSICS_TEST_SOCKET"), "Explicit local PostgreSQL test socket required")
class PostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg2
        cls.conn = psycopg2.connect(dbname="postgres", user="codex", host=os.environ["EVEOSINT_FORENSICS_TEST_SOCKET"], port=55444)
        with cls.conn.cursor() as cur:
            cur.execute(economy.SCHEMA.read_text())
            cur.execute(economy.SCHEMA.read_text())
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "EVEOnline_MER_202608.zip"
        with self.conn.cursor() as cur:
            cur.execute("TRUNCATE mer.region_economy_monthly,mer.global_economy_history,mer.isk_flow_history,mer.economy_unmapped_rows,mer.economy_imports")
        self.conn.commit()

    def scalar(self, query):
        with self.conn.cursor() as cur:
            cur.execute(query)
            result = cur.fetchone()[0]
        self.conn.rollback()
        return result

    def test_manual_cli_persists_progress_and_retry_status(self):
        import psycopg2
        progress_path = Path(self.tmp.name) / "progress.json"
        def connection(_config):
            return psycopg2.connect(dbname="postgres", user="codex", host=os.environ["EVEOSINT_FORENSICS_TEST_SOCKET"], port=55444)
        argv = ["--archive", str(self.path), "--progress-file", str(progress_path)]
        archive(self.path, {"MoneySupply.csv": [["date", "total"], ["2026-08-01", "10"]]})
        with patch.object(economy, "connect", side_effect=connection), patch.object(economy, "load_regions", return_value={}):
            self.assertEqual(economy.main(argv), 0)
            progress = json.loads(progress_path.read_text())
            self.assertEqual((progress["phase"], progress["processed"], progress["facts"]), ("completed", 1, 1))
            self.assertEqual(economy.main(argv), 0)
            self.assertEqual(json.loads(progress_path.read_text())["skipped"], 1)
            archive(self.path, {"MoneySupply.csv": [["date", "total"], ["2026-08-01", "broken"]]})
            self.assertEqual(economy.main(argv), 1)
            progress = json.loads(progress_path.read_text())
            self.assertEqual((progress["phase"], progress["processed"], progress["failed"]), ("failed", 1, 1))

    def test_import_deduplication_latest_report_and_skip(self):
        members = {"MoneySupply.csv": [["date", "total"], ["2026-07-01", "10"]],
                   "RegionalStats.csv": [["regionID", "regionName", "total.production"], ["10000001", "Derelik", "5"]]}
        archive(self.path, members)
        economy.import_archive(self.conn, self.path, MONTH, {})
        self.assertEqual(economy.import_archive(self.conn, self.path, MONTH, {}), {"skipped": 1})
        members["MoneySupply.csv"][1][1] = "20"
        archive(self.path, members)
        economy.import_archive(self.conn, self.path, date(2026, 9, 1), {})
        economy.import_archive(self.conn, self.path, MONTH, {}, force=True)
        self.assertEqual(self.scalar("SELECT value FROM mer.global_economy_history"), 20)
        self.assertEqual(self.scalar("SELECT count(*) FROM mer.global_economy_history"), 1)

    def test_failed_reimport_rolls_back_old_data_and_is_retryable(self):
        archive(self.path, {"MoneySupply.csv": [["date", "total"], ["2026-08-01", "10"]]})
        economy.import_archive(self.conn, self.path, MONTH, {})
        archive(self.path, {"MoneySupply.csv": [["date", "total"], ["2026-08-01", "broken"]]})
        with self.assertRaises(ValueError):
            economy.import_archive(self.conn, self.path, MONTH, {})
        self.assertEqual(self.scalar("SELECT value FROM mer.global_economy_history"), 10)
        self.assertEqual(self.scalar("SELECT status FROM mer.economy_imports"), "failed")
        archive(self.path, {"MoneySupply.csv": [["date", "total"], ["2026-08-01", "12"]]})
        economy.import_archive(self.conn, self.path, MONTH, {})
        self.assertEqual(self.scalar("SELECT value FROM mer.global_economy_history"), 12)

    def test_daily_flows_are_summed_and_not_mixed_with_monthly(self):
        archive(self.path, {
            "SinksFaucets.csv": [["keyText", "Faucet", "Sink"], ["Insurance", "100", "-10"]],
            "sinks_and_faucets_history.csv": [["history_date", "entry_name", "entry_faucet_value", "entry_sink_value"],
                                             ["2026-08-01", "Insurance", "40", "-4"], ["2026-08-02", "Insurance", "60", "-6"]],
        })
        economy.import_archive(self.conn, self.path, MONTH, {})
        self.assertEqual(self.scalar("SELECT value FROM mer.isk_flow_monthly WHERE period_grain='day' AND metric='sink_isk'"), -10)
        self.assertEqual(self.scalar("SELECT count(*) FROM mer.isk_flow_monthly WHERE metric='sink_isk'"), 2)

    def test_stock_boundaries_calendar_growth_and_volatility(self):
        archive(self.path, {"money_supply.csv": [["history_date", "total_isk"],
                                                ["2026-07-31", "100"], ["2026-08-01", "110"],
                                                ["2026-08-02", "121"], ["2026-08-04", "242"]]})
        economy.import_archive(self.conn, self.path, MONTH, {})
        self.assertEqual(self.scalar("SELECT monthly_change_pct FROM mer.global_economy_monthly WHERE month='2026-08-01'"), 142)
        self.assertEqual(self.scalar("SELECT first_observation_date FROM mer.global_economy_monthly WHERE month='2026-08-01'"), date(2026, 8, 1))
        self.assertEqual(self.scalar("SELECT daily_return_volatility_pct FROM mer.global_economy_monthly WHERE month='2026-08-01'"), 0)
        self.assertIsNone(self.scalar("SELECT yearly_change_pct FROM mer.global_economy_monthly WHERE month='2026-08-01'"))

    def test_unknown_economic_csv_is_retained(self):
        archive(self.path, {"future_economy.csv": [["new"], ["hello"]]})
        stats = economy.import_archive(self.conn, self.path, MONTH, {})
        self.assertEqual(stats["unmapped_rows"], 1)
        self.assertEqual(self.scalar("SELECT payload->>'new' FROM mer.economy_unmapped_rows"), "hello")


if __name__ == "__main__":
    unittest.main()
