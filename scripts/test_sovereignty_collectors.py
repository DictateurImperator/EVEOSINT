#!/usr/bin/env python3
"""Offline smoke tests for sovereignty collectors; no HTTP or DB access."""
import unittest

from scripts.sync_sovereignty_esi import build_changes, normalize
from scripts.import_sovereignty_dotlan import classification, ownership_model, parse_events
from scripts.sovereignty_scope import claimable_sov_systems_from_rows


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


class EsiDeltaTests(unittest.TestCase):
    def test_only_real_changes_are_emitted(self):
        from datetime import datetime, timezone

        observed = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
        previous = {
            30000001: (1001, None, None),
            30000002: (1002, None, None),
            30000003: (1003, None, None),
        }
        current = {
            30000001: (1001, None, None),
            30000002: (2002, None, None),
            30000004: (1004, None, None),
        }

        changes = build_changes(previous, current, observed)
        by_system = {}
        for row in changes:
            by_system.setdefault(row[0], []).append(row)

        self.assertEqual(set(by_system), {30000002, 30000003, 30000004})
        self.assertEqual([row[1] for row in by_system[30000002]], ["LOST", "GAIN"])
        self.assertEqual(by_system[30000002][0][2:8], (1002, None, None, None, None, None))
        self.assertEqual(by_system[30000002][1][2:8], (None, None, None, 2002, None, None))
        self.assertEqual(by_system[30000003][0][1], "LOST")
        self.assertEqual(by_system[30000003][0][2:8], (1003, None, None, None, None, None))
        self.assertEqual(by_system[30000004][0][1], "GAIN")
        self.assertEqual(by_system[30000004][0][2:8], (None, None, None, 1004, None, None))
        self.assertTrue(all(row[8] == observed for row in changes))
        self.assertNotIn("TRANSFER", [row[1] for row in changes])

    def test_unchanged_map_produces_no_history(self):
        from datetime import datetime, timezone

        observed = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
        state = {
            30000001: (1001, None, None),
            30000002: (None, None, 500001),
        }
        self.assertEqual(build_changes(state, dict(state), observed), [])


class SovereigntyScopeTests(unittest.TestCase):
    def test_only_conquerable_nullsec_is_kept(self):
        regions = [
            (10000001, {"_key": 10000001, "name": {"en": "Claimable"}, "wormholeClassID": 9}),
            (10000002, {"_key": 10000002, "name": {"en": "NPC"}, "factionID": 500001}),
            (10000070, {"_key": 10000070, "name": {"en": "Pochven"}}),
            (11000001, {"_key": 11000001, "name": {"en": "J-Space"}, "wormholeClassID": 6}),
        ]
        constellations = [
            (20000001, {"_key": 20000001, "regionID": 10000001}),
            (20000002, {"_key": 20000002, "regionID": 10000001, "factionID": 500002}),
            (20000003, {"_key": 20000003, "regionID": 10000002}),
            (20000004, {"_key": 20000004, "regionID": 10000070}),
            (21000001, {"_key": 21000001, "regionID": 11000001}),
        ]
        systems = [
            (30000001, {"_key": 30000001, "name": {"en": "Claimable A"}, "regionID": 10000001, "constellationID": 20000001, "securityStatus": -0.5}),
            (30000002, {"_key": 30000002, "name": {"en": "High"}, "regionID": 10000001, "constellationID": 20000001, "securityStatus": 0.5}),
            (30000003, {"_key": 30000003, "name": {"en": "NPC Pocket"}, "regionID": 10000001, "constellationID": 20000002, "securityStatus": -0.2}),
            (30000004, {"_key": 30000004, "name": {"en": "NPC Region"}, "regionID": 10000002, "constellationID": 20000003, "securityStatus": -0.7}),
            (30000005, {"_key": 30000005, "name": {"en": "System Faction"}, "regionID": 10000001, "constellationID": 20000001, "securityStatus": -0.4, "factionID": 500003}),
            (30000006, {"_key": 30000006, "name": {"en": "Pochven"}, "regionID": 10000070, "constellationID": 20000004, "securityStatus": -1.0}),
            (31000001, {"_key": 31000001, "name": {"en": "Wormhole"}, "regionID": 11000001, "constellationID": 21000001, "securityStatus": -1.0}),
        ]
        result = claimable_sov_systems_from_rows(systems, constellations, regions)
        self.assertEqual(set(result), {30000001})


class OwnershipConventionTests(unittest.TestCase):
    def test_eveosint_ownership_eras(self):
        from datetime import datetime

        self.assertEqual(ownership_model(datetime(2015, 7, 13, 23, 59)), "legacy_sov")
        self.assertEqual(ownership_model(datetime(2015, 7, 14, 0, 0)), "ihub_proxy")
        self.assertEqual(ownership_model(datetime(2024, 6, 11, 0, 0)), "sovhub_legacy_ihub_proxy")
        self.assertEqual(ownership_model(datetime(2024, 6, 27, 0, 0)), "ihub_sovhub_transition_proxy")
        self.assertEqual(ownership_model(datetime(2024, 10, 29, 0, 0)), "sovhub")


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
        self.assertEqual(parsed[0]["ownership_model"], "ihub_proxy")

    def test_transfer_becomes_lost_then_gain(self):
        html = """<html><h2>Sovereignty Changes [3]</h2>
          <table>
          <tr><th>Date</th><th>Time</th><th>Action</th>
              <th>Alliance</th><th>Corporation</th></tr>
          <tr><td>2025-10-13</td><td>11:26</td><td>Transfer</td><td></td>
              <td><a href="/alliance/New_Alliance">New Alliance</a></td><td></td>
              <td><a href="/corp/New_Corp">New Corp</a></td></tr>
          <tr><td>2025-09-19</td><td>12:51</td><td>4
              <img alt="-&gt;"/>5</td><td></td>
              <td><a href="/alliance/Old_Alliance">Old Alliance</a></td><td></td>
              <td><a href="/corp/Old_Corp">Old Corp</a></td></tr>
          <tr><td>2025-06-12</td><td>11:05</td><td>Gain</td><td></td>
              <td><a href="/alliance/Old_Alliance">Old Alliance</a></td><td></td>
              <td><a href="/corp/Old_Corp">Old Corp</a></td></tr>
          </table></html>"""

        parsed = parse_events(
            html,
            30000001,
            "https://evemaps.dotlan.net/system/TEST",
        )
        transfer_rows = [
            row for row in parsed
            if row["action_raw"].lower() == "transfer"
        ]

        self.assertEqual([row["action"] for row in transfer_rows], ["LOST", "GAIN"])
        self.assertEqual(transfer_rows[0]["alliance_name"], "Old Alliance")
        self.assertEqual(transfer_rows[0]["corporation_name"], "Old Corp")
        self.assertEqual(transfer_rows[1]["alliance_name"], "New Alliance")
        self.assertEqual(transfer_rows[1]["corporation_name"], "New Corp")
        self.assertNotIn("TRANSFER", [row["action"] for row in parsed])

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
