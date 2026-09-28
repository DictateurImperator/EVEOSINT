#!/usr/bin/env python3
"""Offline smoke tests for sovereignty collectors; no HTTP or DB access."""
import unittest

from scripts.sync_sovereignty_esi import normalize
from scripts.import_sovereignty_dotlan import classification, parse_events


class EsiSnapshotTests(unittest.TestCase):
    def test_map_validation(self):
        rows = normalize([
            {"system_id": 30004759, "alliance_id": 1354830081},
            {"system_id": 30004760, "corporation_id": 98000001, "faction_id": None},
        ])
        self.assertEqual(rows[30004759], (1354830081, None, None))
        self.assertEqual(rows[30004760], (None, 98000001, None))

    def test_reject_empty_or_partial_corruption(self):
        for payload in ([], {}, [{"system_id": 3}], [
            {"system_id": 1, "faction_id": 5},
            {"system_id": 1, "faction_id": 6},
        ]):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                normalize(payload)


class DotlanSourceTests(unittest.TestCase):
    SAMPLE = """<html><h2>Sovereignty Changes [3]</h2>
      <table>
      <tr><th>Date</th><th>Time</th><th>Action</th>
          <th>Alliance</th><th>Corporation</th></tr>
      <tr><td></td><td>2024-01-03</td><td>12:15</td><td>Gain</td><td></td>
          <td><a href="/alliance/Test_Alliance">Test Alliance</a></td><td></td>
          <td><a href="/corp/Test_Corp">Test Corp</a></td></tr>
      <tr><td></td><td>2024-01-02</td><td>12:15</td><td>Lost</td><td></td>
          <td><a href="/alliance/Previous">Previous</a></td><td></td>
          <td>Holding Corp</td></tr>
      <tr><td></td><td>2024-01-01</td><td>12:15</td><td>4
          <img alt="-&gt;"/>5</td><td></td><td>Previous</td><td></td><td></td></tr>
      </table></html>"""

    def test_extracts_all_actions_and_keeps_source(self):
        url = "https://evemaps.dotlan.net/system/TEST"
        parsed = parse_events(self.SAMPLE, 30000001, url)
        self.assertEqual(len(parsed), 3)
        self.assertEqual([row["action"] for row in parsed], ["GAIN", "LOST", "LEVEL_CHANGE"])
        self.assertEqual(parsed[0]["alliance_name"], "Test Alliance")
        self.assertEqual(parsed[0]["alliance_url"], "https://evemaps.dotlan.net/alliance/Test_Alliance")
        self.assertEqual(parsed[0]["corporation_name"], "Test Corp")
        self.assertEqual(parsed[1]["corporation_name"], "Holding Corp")
        self.assertEqual(parsed[0]["event_hash"], parse_events(self.SAMPLE, 30000001, url)[0]["event_hash"])

    def test_no_inferred_owner_when_no_link(self):
        parsed = parse_events(self.SAMPLE, 30000001, "https://evemaps.dotlan.net/system/TEST")
        self.assertIsNone(parsed[1]["corporation_url"])
        self.assertIsNone(parsed[2]["alliance_url"])

    def test_rejects_unexpected_layout_or_truncation(self):
        bad = self.SAMPLE.replace("Changes [3]", "Changes [4]")
        with self.assertRaises(ValueError):
            parse_events(bad, 30000001, "https://evemaps.dotlan.net/system/TEST")
        with self.assertRaises(ValueError):
            parse_events("<html>Unexpected page</html>", 30000001, "https://evemaps.dotlan.net/system/TEST")

    def test_legitimate_empty_table(self):
        parsed = parse_events("<h2>Sovereignty Changes [0]</h2>", 30000001, "https://evemaps.dotlan.net/system/TEST")
        self.assertEqual(parsed, [])
        self.assertEqual(classification("3 -> 4"), "LEVEL_CHANGE")


if __name__ == "__main__":
    unittest.main()
