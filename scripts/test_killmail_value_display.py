"""Stored values reach shared killboard rows without touching raw data."""

import unittest
from decimal import Decimal
from unittest.mock import patch

from scripts import test_killmail_forensics as f


class ValueDisplayTests(unittest.TestCase):
    def test_cents_stay_exact_including_zero_and_large_values(self):
        for cents, label, full in [
            (0, "0", "0.00 ISK"),
            (1, "0.01", "0.01 ISK"),
            (123456, "1.23 K", "1,234.56 ISK"),
            (9150000005, "91.5 M", "91,500,000.05 ISK"),
            (250000000000, "2.5 B", "2,500,000,000.00 ISK"),
            (340000000000000, "3.4 T", "3,400,000,000,000.00 ISK"),
            (9223372036854775807, "92,233.72 T", "92,233,720,368,547,758.07 ISK"),
        ]:
            with self.subTest(cents=cents):
                result = f.entities._killmail_value_fields(cents)
                self.assertEqual(result["value_label"], label)
                self.assertEqual(result["value_full_label"], full)
                self.assertEqual(result["value_source"], "calculated")

    def test_missing_value_is_not_presented_as_zero(self):
        result = f.entities._killmail_value_fields()
        self.assertEqual(result["value_label"], "—")
        self.assertIsNone(result["value_full_label"])
        self.assertIsNone(result["value_source"])

    def test_mer_value_keeps_its_source_and_exact_amount(self):
        result = f.entities._killmail_value_fields(mer_isk=Decimal("987654321.23"))
        self.assertEqual(result["value_label"], "987.65 M")
        self.assertEqual(result["value_full_label"], "987,654,321.23 ISK")
        self.assertEqual(result["value_source"], "mer")
        self.assertIn("MER", result["value_title"])

    def test_global_hydration_reads_the_existing_value_in_its_detail_query(self):
        # Same rows for progressive and ordinary global killboards.
        rows = [(i, i + 1, f.TIME, 30000142, 638, 1000, 90000001, "Pilot",
                 100001, "Victim Corp", "TEST", None, None, None, cents)
                for i, cents in enumerate((9150000005, 0, None))]
        conn = f.Connection(rows)
        with f.fixture_db(conn):
            result = f.entities._hydrate_global_killmail_entries(
                conn, [("kill", i + 1, f.TIME) for i in range(3)])
        self.assertEqual([r["value_label"] for r in result], ["91.5 M", "0", "—"])
        sql = next(sql for sql, _params in conn.queries if "AS estimated_value_cents" in sql)
        self.assertIn("to_jsonb(km)->>'estimated_value_cents'", sql)
        self.assertIn("km.killmail_time >= %s", sql)
        self.assertIn("km.killmail_time < %s", sql)
        self.assertEqual(sum("AS estimated_value_cents" in sql for sql, _ in conn.queries), 1)

    def test_character_and_alliance_lists_show_the_stored_amount(self):
        class Connection(f.Connection):
            def execute(self, sql, params=None):
                super().execute(sql, params)
                if "AS estimated_value_cents" in sql:
                    self.rows = [("loss", 0, 1, f.TIME, 30000142, 638, 1000,
                                  90000001, "Pilot", 100001, "Victim Corp", "TEST",
                                  None, None, None, 9150000005)]
                elif "WHERE victim_character_id = %s" in sql:
                    self.rows = [("loss", 1, f.TIME)]
                else:
                    self.rows = []

        for kind in ("character", "alliance"):
            with self.subTest(kind=kind):
                conn = Connection()
                with f.fixture_db(conn), patch.object(f.entities, "_entity_killmail_page_entries",
                        return_value=([("loss", 1, f.TIME)], False, 1, 100)), patch("builtins.print"):
                    if kind == "character":
                        rows, _ = f.entities._character_killmails(conn, 90000001)
                    else:
                        rows, _ = f.entities._group_entity_killmails(conn, "alliance", 90000002)
                self.assertEqual(rows[0]["value_label"], "91.5 M")
                self.assertEqual(rows[0]["value_full_label"], "91,500,000.05 ISK")


if __name__ == "__main__":
    unittest.main()
