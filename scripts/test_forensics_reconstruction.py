"""Offline engine/route tests and optional PostgreSQL integration in a disposable lab.

EVEOSINT_FORENSICS_TEST_SOCKET=/tmp/eveosint-forensics-lab python -m unittest
scripts.test_forensics_reconstruction -v
The optional integration uses only a socket under /tmp on port 55444.
"""

import hashlib
import os
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import psycopg2

from scripts import test_killmail_forensics as f

engine = __import__(f.PACKAGE + ".forensics_engine", fromlist=["*"])
evidence = __import__(f.PACKAGE + ".forensics_evidence", fromlist=["*"])
store = f.routes.forensics_store
AT = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
SNAPSHOT = {
    "kill_datetime": AT.isoformat(),
    "source_month": "2026-08-01",
    "source_row": 2,
    "victim_ship_type_id": 587,
    "killer_ship_type_id": 588,
    "victim_corporation_id": 100,
    "killer_corporation_id": 200,
    "solar_system_id": 30000142,
}


def payload(victim=10, attacker=21):
    return {
        "killmail_id": 1001,
        "killmail_time": AT.isoformat(),
        "solar_system_id": 30000142,
        "victim": {
            "ship_type_id": 587,
            "corporation_id": 100,
            "damage_taken": 200,
            **({"character_id": victim} if victim else {}),
        },
        "attackers": [
            {
                "final_blow": True,
                "ship_type_id": 588,
                "corporation_id": 200,
                "damage_done": 200,
                **({"character_id": attacker} if attacker else {}),
            }
        ],
    }


class EngineTests(unittest.TestCase):
    def test_hash_known_vector_timezone_and_absent_character(self):
        self.assertEqual(
            engine.killmail_hash(1, 2, 3, "1970-01-01T00:00:00+00:00"),
            hashlib.sha1(b"123116444736000000000").hexdigest(),
        )
        self.assertEqual(
            engine.killmail_hash(None, None, 3, "1970-01-01T01:00:00+01:00"),
            hashlib.sha1(b"NoneNone3116444736000000000").hexdigest(),
        )
        for stamp in ("1970-01-01T00:00:00", "1970-01-01T00:00:00.1+00:00"):
            with self.assertRaises(ValueError):
                engine.killmail_hash(1, 2, 3, stamp)

    def test_id_rank_ties_legacy_gaps_and_known_ids(self):
        before = {"id": 1000, "time": "before"}
        after = {"id": 1005, "time": "after"}
        exact = engine.infer_ids(before, after, 2, 1, 4, [])
        self.assertEqual([c["id"] for c in exact["candidates"]], [1003])
        self.assertTrue(exact["deduced"])
        ties = engine.infer_ids(before, after, 1, 2, 4, [1002])
        self.assertEqual([c["id"] for c in ties["candidates"]], [1003])
        legacy = engine.infer_ids(before, after, 0, 1, 1, [])
        self.assertEqual(
            [c["id"] for c in legacy["candidates"]], [1001, 1002, 1003, 1004]
        )
        wide = engine.infer_ids(before, {"id": 2000, "time": "after"}, 0, 1, 2, [])
        self.assertTrue(wide["truncated"])
        self.assertEqual(wide["total"], 999)

    def test_historical_usage_unique_ship_and_habits_raise_rank(self):
        event = {
            "killmail_id": 900,
            "character_id": 10,
            "corporation_id": 100,
            "ship_type_id": 587,
            "system_id": 30000142,
            "time": AT - timedelta(minutes=3),
            "role": "attacker",
            "final_blow": True,
        }
        result = engine.pilot_candidates(
            SNAPSHOT,
            "victim",
            {10: "A", 11: "B"},
            [event],
            {11},
            Counter({(10, 11): 5}),
            prior_usage={(10, 587): AT - timedelta(days=2)},
        )
        self.assertEqual(result["candidates"][0]["id"], 10)
        self.assertTrue(
            any("Previously flew" in r for r in result["candidates"][0]["reasons"])
        )
        self.assertTrue(
            any("Only one observed" in r for r in result["candidates"][0]["reasons"])
        )
        b = next(c for c in result["candidates"] if c["id"] == 11)
        self.assertTrue(any("not proof" in r for r in b["reasons"]))

    def test_disappearance_needs_a_continuing_fight(self):
        events = [
            {
                "killmail_id": 1,
                "character_id": 10,
                "corporation_id": 100,
                "ship_type_id": 587,
                "system_id": 30000142,
                "time": AT - timedelta(minutes=2),
                "role": "attacker",
            }
        ]
        for index in (2, 3):
            events.append(
                {
                    **events[0],
                    "character_id": 11,
                    "killmail_id": index,
                    "time": AT + timedelta(minutes=index * 4),
                }
            )
        result = engine.pilot_candidates(
            SNAPSHOT, "victim", {10: "A", 11: "B"}, events, set(), {}
        )
        c = next(c for c in result["candidates"] if c["id"] == 10)
        self.assertTrue(any("Disappears" in r for r in c["reasons"]))

    def test_same_ship_combat_then_loss_is_generic_and_npc_adds_evidence(self):
        for ship in (587, 16240, 23919):
            snapshot = {**SNAPSHOT, "victim_ship_type_id": ship}
            event = {
                "killmail_id": 1,
                "character_id": 10,
                "corporation_id": 100,
                "ship_type_id": ship,
                "system_id": 30000142,
                "time": AT - timedelta(minutes=1),
                "role": "attacker",
            }
            ordinary = engine.pilot_candidates(
                snapshot, "victim", {10: "A", 11: "B"}, [event], set(), {}
            )
            npc = engine.pilot_candidates(
                snapshot,
                "victim",
                {10: "A", 11: "B"},
                [event],
                set(),
                {},
                npc_loss=True,
            )
            first = ordinary["candidates"][0]
            self.assertEqual(first["id"], 10)
            self.assertTrue(
                any("same ship and corporation" in r for r in first["reasons"])
            )
            self.assertGreater(npc["candidates"][0]["weight"], first["weight"])

    def test_capsule_transitions_are_ranked_but_owned_objects_are_not_ship_losses(self):
        pod = {**SNAPSHOT, "victim_ship_type_id": 670}
        ship_loss = {
            "killmail_id": 900,
            "character_id": 10,
            "corporation_id": 100,
            "ship_type_id": 587,
            "ship_category_id": 6,
            "system_id": 30000142,
            "time": AT - timedelta(seconds=90),
            "role": "victim",
        }
        result = engine.pilot_candidates(
            pod, "victim", {10: "A", 11: "B"}, [ship_loss], set(), {}, category=6
        )
        self.assertEqual(result["candidates"][0]["id"], 10)
        self.assertTrue(
            any("Known ship loss 90s" in r for r in result["candidates"][0]["reasons"])
        )
        later = {**ship_loss, "ship_type_id": 670, "time": AT + timedelta(seconds=24)}
        result = engine.pilot_candidates(
            SNAPSHOT, "victim", {10: "A", 11: "B"}, [later], set(), {}, category=6
        )
        self.assertEqual(result["candidates"][0]["id"], 10)
        self.assertTrue(
            any(
                "Known capsule loss 24s" in r
                for r in result["candidates"][0]["reasons"]
            )
        )
        for event in (
            {**ship_loss, "ship_category_id": 22},
            {**ship_loss, "time": AT - timedelta(minutes=11)},
            {**ship_loss, "corporation_id": 200},
            {**ship_loss, "system_id": 30000143},
        ):
            result = engine.pilot_candidates(
                pod, "victim", {10: "A", 11: "B"}, [event], set(), {}, category=6
            )
            self.assertFalse(
                any(
                    "capsule-related" in r
                    for c in result["candidates"]
                    for r in c["reasons"]
                )
            )

    def test_owned_object_losses_do_not_imply_owner_death(self):
        snapshot = {**SNAPSHOT, "victim_ship_type_id": 33475}
        loss = {
            "killmail_id": 900,
            "character_id": 10,
            "corporation_id": 100,
            "ship_type_id": 33475,
            "ship_category_id": 22,
            "system_id": 30000142,
            "time": AT - timedelta(minutes=1),
            "role": "victim",
        }
        result = engine.pilot_candidates(
            snapshot, "victim", {10: "Owner"}, [loss], set(), {}, category=22
        )
        owner = next(c for c in result["candidates"] if c["id"] == 10)
        self.assertTrue(
            any("does not imply the owner died" in r for r in owner["reasons"])
        )
        self.assertFalse(any("reshipping" in r for r in owner["reasons"]))

    def test_known_combatants_prioritized_without_excluding_unknown_members(self):
        result = engine.pilot_candidates(
            SNAPSHOT,
            "attacker",
            {20: "Active", 21: "No known attacks"},
            [],
            set(),
            {},
            past_pvp={20: AT - timedelta(days=2)},
        )
        active = next(c for c in result["candidates"] if c["id"] == 20)
        unknown = next(c for c in result["candidates"] if c["id"] == 21)
        self.assertTrue(active["pvp_priority"])
        self.assertFalse(unknown["pvp_priority"])
        self.assertGreater(active["weight"], unknown["weight"])
        self.assertTrue(any("unselected initially" in r for r in unknown["reasons"]))

    def test_npc_corp_is_not_treated_as_proof_of_absent_pilot(self):
        result = engine.pilot_candidates(
            SNAPSHOT, "victim", {10: "Player"}, [], set(), {}, category=65
        )
        self.assertEqual({c["id"] for c in result["candidates"]}, {10, None})
        absent = next(c for c in result["candidates"] if c["id"] is None)
        self.assertTrue(any("do not prove" in r for r in absent["reasons"]))
        self.assertTrue(
            engine.matches_snapshot(payload(None, None), SNAPSHOT, 1001, None, None)
        )

    def test_payload_match_requires_id_ship_time_location_roles_and_corps(self):
        self.assertTrue(engine.matches_snapshot(payload(), SNAPSHOT, 1001, 10, 21))
        for key, value in (
            ("killmail_id", 1002),
            ("killmail_time", (AT + timedelta(seconds=1)).isoformat()),
            ("solar_system_id", 30000143),
        ):
            data = payload()
            data[key] = value
            self.assertFalse(engine.matches_snapshot(data, SNAPSHOT, 1001, 10, 21))
        data = payload()
        data["attackers"][0]["final_blow"] = False
        self.assertFalse(engine.matches_snapshot(data, SNAPSHOT, 1001, 10, 21))
        data = payload()
        data["attackers"][0]["corporation_id"] = 201
        self.assertFalse(engine.matches_snapshot(data, SNAPSHOT, 1001, 10, 21))

    def test_npc_optional_affiliations_do_not_reject_a_valid_hash(self):
        data = payload(10, None)
        del data["attackers"][0]["corporation_id"]
        del data["attackers"][0]["ship_type_id"]
        self.assertTrue(engine.matches_snapshot(data, SNAPSHOT, 1001, 10, None))

    def test_combination_count_and_input_limits(self):
        hypotheses = {k: {"candidates": []} for k in ("ids", "victim", "attacker")}
        choices = engine.normalize_choices(
            {"ids": [1001], "victims": [10, 11], "attackers": [20, 21, 22, 23, 24]}
        )
        self.assertEqual(len(engine.combinations(choices, hypotheses)), 10)
        with self.assertRaises(ValueError):
            engine.normalize_choices({"ids": [True], "victims": [], "attackers": []})
        with self.assertRaises(ValueError):
            engine.combinations(
                {k: list(range(200)) for k in ("ids", "victims", "attackers")},
                hypotheses,
            )

    def test_header_backoff_new_and_legacy_limits(self):
        self.assertEqual(store.retry_seconds({"Retry-After": "12"}, 429), 12)
        self.assertEqual(
            store.retry_seconds(
                {"X-Ratelimit-Remaining": "9", "X-Ratelimit-Limit": "3600/15m"}, 200
            ),
            900,
        )
        self.assertEqual(
            store.retry_seconds(
                {"X-ESI-Error-Limit-Remain": "5", "X-ESI-Error-Limit-Reset": "42"}, 403
            ),
            43,
        )
        self.assertGreaterEqual(store.retry_seconds({}, 420), 60)


class WorkspaceEndpointTests(f.EndpointTests):
    def test_setup_gate_never_creates_tables(self):
        with (
            patch.object(store, "ready", return_value=False),
            patch.object(store, "create_case") as create,
        ):
            response = self.client.post(
                "/admin/killmail-forensics/cases", json=SNAPSHOT
            )
            self.assertEqual(response.status_code, 503)
            self.assertTrue(response.json()["setup_required"])
            create.assert_not_called()

    def test_write_routes_reject_cross_origin_and_missing_dev_permission(self):
        with (
            patch.object(store, "ready", return_value=True),
            patch.object(store, "save_choices") as save,
        ):
            response = self.client.post(
                "/admin/killmail-forensics/cases/1/choices",
                json={"ids": [1001], "victims": [10], "attackers": [20]},
                headers={"Origin": "https://evil.example"},
            )
            self.assertEqual(response.status_code, 403)
            save.assert_not_called()
            self.user = {"permissions": {"entities.view"}}
            response = self.client.post(
                "/admin/killmail-forensics/cases/1/validate", json={}
            )
            self.assertEqual(response.status_code, 403)


class MigrationJobTests(unittest.TestCase):
    def test_job_registration_is_passive_and_launch_uses_the_checked_in_script(self):
        import sys

        config = sys.modules[f.PACKAGE + ".config"]
        with patch.object(
            config, "JOBS_CONFIG_PATH", f.ROOT / "config/offline-jobs.json", create=True
        ):
            jobs = f.load("jobs")
        with (
            patch.object(jobs, "_read_config_file", return_value={"jobs": []}),
            patch.object(
                jobs,
                "_read_runtime_status",
                return_value={"running": False, "pid": None, "started_at": None},
            ),
            patch.object(
                jobs, "_start_process", return_value=(True, "started")
            ) as start,
        ):
            registered = jobs.get_job("setup_killmail_forensics")
            self.assertEqual(registered["type"], "killmail_forensics_setup")
            self.assertEqual(
                registered["command"][1],
                str(f.ROOT / "scripts/setup_killmail_forensics.py"),
            )
            start.assert_not_called()
            self.assertEqual(jobs.run_killmail_forensics_setup_job(), (True, "started"))
            start.assert_called_once_with(registered, registered["command"])


SOCKET = os.environ.get("EVEOSINT_FORENSICS_TEST_SOCKET", "")


@unittest.skipUnless(
    SOCKET.startswith("/tmp/"),
    "Set EVEOSINT_FORENSICS_TEST_SOCKET to a disposable lab socket under /tmp",
)
class PersistenceTests(unittest.TestCase):
    def connect(self):
        return psycopg2.connect(
            host=SOCKET, port=55444, dbname="postgres", user="codex"
        )

    def setUp(self):
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DROP SCHEMA IF EXISTS web CASCADE; DROP SCHEMA IF EXISTS mer CASCADE; DROP SCHEMA IF EXISTS rawkm CASCADE; DROP SCHEMA IF EXISTS entities CASCADE; CREATE SCHEMA web; CREATE SCHEMA mer; CREATE SCHEMA rawkm; CREATE SCHEMA entities;"
                )
                ddl = (f.ROOT / "web/app/forensics_schema.sql").read_text()
                cur.execute(ddl)
                cur.execute(ddl)
                cur.execute("""CREATE TABLE mer.killmails(source_month date,source_row bigint,kill_datetime timestamptz,
                    victim_ship_type_id bigint,killer_ship_type_id bigint,victim_corporation_id bigint,killer_corporation_id bigint,
                    solar_system_id bigint,resolved_km bigint[],resolved_km_ambiguous boolean DEFAULT false,
                    PRIMARY KEY(kill_datetime,source_month,source_row));
                    CREATE TABLE rawkm.killmails(killmail_id bigint,killmail_time timestamptz,solar_system_id bigint,
                        victim_character_id bigint,victim_corporation_id bigint,victim_ship_type_id bigint);
                    CREATE TABLE rawkm.killmail_attackers(killmail_id bigint,killmail_time timestamptz,character_id bigint,
                        corporation_id bigint,ship_type_id bigint,final_blow boolean);
                    CREATE TABLE entities.characters(character_id bigint,name text);
                    CREATE TABLE entities.character_corporation_history(character_id bigint,corporation_id bigint,start_date timestamptz,end_date timestamptz,is_deleted boolean DEFAULT false);""")
                for row, at, ids in (
                    (1, AT - timedelta(seconds=10), [1000]),
                    (2, AT, None),
                    (3, AT + timedelta(seconds=10), [1002]),
                ):
                    cur.execute(
                        "INSERT INTO mer.killmails VALUES(%s,%s,%s,587,588,100,200,30000142,%s,false)",
                        ("2026-08-01", row, at, ids),
                    )
                for cid, corp, ship in (
                    (10, 100, 587),
                    (11, 100, 589),
                    (20, 200, 588),
                    (21, 200, 589),
                ):
                    cur.execute(
                        "INSERT INTO entities.characters VALUES(%s,%s)",
                        (cid, f"Pilot {cid}"),
                    )
                    cur.execute(
                        "INSERT INTO entities.character_corporation_history VALUES(%s,%s,%s,NULL,false)",
                        (cid, corp, AT - timedelta(days=100)),
                    )
                    cur.execute(
                        "INSERT INTO rawkm.killmails VALUES(%s,%s,30000142,NULL,NULL,589)",
                        (900 + cid, AT - timedelta(minutes=5)),
                    )
                    cur.execute(
                        "INSERT INTO rawkm.killmail_attackers VALUES(%s,%s,%s,%s,%s,true)",
                        (900 + cid, AT - timedelta(minutes=5), cid, corp, ship),
                    )
        self.patch = patch.object(store, "db", self.connect)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.case = store.create_case(
            {
                "kill_datetime": AT.isoformat(),
                "source_month": "2026-08-01",
                "source_row": 2,
            },
            7,
        )
        store.save_choices(
            self.case["id"], {"ids": [1001], "victims": [10, 11], "attackers": [20, 21]}
        )

    def reset_gate(self):
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE web.forensics_esi_gate SET blocked_until=NOW()-INTERVAL '1 second'"
                )

    def test_migration_repeatability_evidence_and_persistent_choices(self):
        self.assertTrue(store.ready())
        self.assertEqual(self.case["hypotheses"]["ids"]["candidates"][0]["id"], 1001)
        self.assertEqual(self.case["hypotheses"]["victim"]["candidates"][0]["id"], 10)
        cases = store.list_cases()
        self.assertEqual(cases[0]["estimated_attempts"], 4)
        duplicate = store.create_case(
            {
                "kill_datetime": AT.isoformat(),
                "source_month": "2026-08-01",
                "source_row": 2,
            },
            7,
        )
        self.assertEqual(duplicate["id"], self.case["id"])
        manual = store.save_choices(
            self.case["id"],
            {"ids": [1001, 1003], "victims": [10, None], "attackers": [20]},
        )
        self.assertEqual(manual["estimated_attempts"], 4)
        self.assertTrue(
            any(c["id"] == 1003 for c in manual["hypotheses"]["ids"]["candidates"])
        )

    def test_blind_evaluation_does_not_read_target_pilots_or_its_known_id(self):
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE mer.killmails SET resolved_km=ARRAY[1001]::bigint[] WHERE source_row=2"
                )
                cur.execute(
                    "INSERT INTO rawkm.killmails VALUES(1001,%s,30000142,99,100,587)",
                    (AT,),
                )
                cur.execute(
                    "INSERT INTO rawkm.killmail_attackers VALUES(1001,%s,98,200,588,true)",
                    (AT,),
                )
            snapshot = store.execute(
                conn,
                "SELECT to_jsonb(m) AS s FROM mer.killmails m WHERE source_row=2",
                one=True,
            )["s"]
            _, hypotheses = evidence.analyze_snapshot(
                conn, snapshot, exclude_kill_ids=[1001]
            )
            self.assertEqual(hypotheses["ids"]["candidates"][0]["id"], 1001)
            self.assertNotIn(99, [c["id"] for c in hypotheses["victim"]["candidates"]])
            self.assertNotIn(
                98, [c["id"] for c in hypotheses["attacker"]["candidates"]]
            )

    def test_validation_invalid_cache_success_and_consultation(self):
        correct = engine.killmail_hash(10, 21, 587, AT)

        def fetch(kill_id, hash_value):
            return (
                (200, {}, payload())
                if hash_value == correct
                else (404, {}, {"error": "Invalid killmail hash"})
            )

        with patch.object(store, "fetch_ccp", side_effect=fetch) as http:
            for _ in range(5):
                self.reset_gate()
                result = store.validate_next(self.case["id"], 7)
                if result.get("done"):
                    break
            self.assertTrue(result["recovered"])
            calls = http.call_count
            self.assertTrue(store.validate_next(self.case["id"], 7)["recovered"])
            self.assertEqual(http.call_count, calls)
        recovered = store.list_cases(recovered=True)[0]
        self.assertEqual(recovered["recovered_hash"], correct)
        self.assertEqual(
            store.recovered_detail(1001)["payload"]["victim"]["character_id"], 10
        )
        self.assertEqual(store.list_cases(), [])

    def test_transient_rate_limit_does_not_consume_hypothesis(self):
        with patch.object(
            store, "fetch_ccp", return_value=(429, {"Retry-After": "12"}, {})
        ):
            self.reset_gate()
            result = store.validate_next(self.case["id"], 7)
        self.assertFalse(result["done"])
        case = store.list_cases()[0]
        self.assertEqual(case["estimated_attempts"], 4)
        self.assertEqual(case["attempted"], 0)
        request, wait = store.reserve_request()
        self.assertIsNone(request)
        self.assertGreater(wait, 0)

    def test_no_false_recovery_and_exhaustion_resume_without_repeat(self):
        wrong = payload()
        wrong["solar_system_id"] = 30000143
        with patch.object(store, "fetch_ccp", return_value=(200, {}, wrong)) as http:
            for _ in range(5):
                self.reset_gate()
                result = store.validate_next(self.case["id"], 7)
                if result["done"]:
                    break
            self.assertFalse(result["recovered"])
            self.assertEqual(http.call_count, 4)
            result = store.validate_next(self.case["id"], 7)
            self.assertTrue(result["done"])
            self.assertEqual(http.call_count, 4)
        case = store.list_cases()[0]
        self.assertEqual(case["status"], "exhausted")
        self.assertEqual(case["estimated_attempts"], 0)
        case = store.save_choices(self.case["id"], case["choices"])
        self.assertEqual(case["estimated_attempts"], 0)

    def test_partial_plan_edit_refresh_retains_attempts_and_correct_remaining_count(
        self,
    ):
        with patch.object(store, "fetch_ccp", return_value=(404, {}, {})) as http:
            self.reset_gate()
            store.validate_next(self.case["id"], 7)
            case = store.list_cases()[0]
            self.assertEqual(case["estimated_attempts"], 3)
            updated = store.save_choices(case["id"], case["choices"])
            self.assertEqual(updated["estimated_attempts"], 3)
            refreshed = store.refresh_case(case["id"])
            self.assertEqual(refreshed["estimated_attempts"], 3)
            self.reset_gate()
            store.validate_next(case["id"], 7)
            self.assertEqual(http.call_count, 2)
            self.assertEqual(store.list_cases()[0]["estimated_attempts"], 2)

    def test_known_recovery_is_reused_for_duplicate_mer_references_without_http(self):
        store.save_choices(
            self.case["id"], {"ids": [1001], "victims": [10], "attackers": [21]}
        )
        with patch.object(store, "fetch_ccp", return_value=(200, {}, payload())):
            self.reset_gate()
            self.assertTrue(store.validate_next(self.case["id"], 7)["recovered"])
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO mer.killmails SELECT source_month,4,kill_datetime,victim_ship_type_id,killer_ship_type_id,victim_corporation_id,killer_corporation_id,solar_system_id,NULL,false FROM mer.killmails WHERE source_row=2"
                )
        second = store.create_case({**SNAPSHOT, "source_row": 4}, 7)
        store.save_choices(
            second["id"], {"ids": [1001], "victims": [10], "attackers": [21]}
        )
        with patch.object(store, "fetch_ccp") as http:
            result = store.validate_next(second["id"], 7)
            self.assertTrue(result["recovered"])
            http.assert_not_called()
        self.assertEqual(len(store.list_cases(recovered=True)), 2)

    def test_initial_attacker_choices_skip_members_without_recorded_attacks(self):
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM rawkm.killmail_attackers WHERE character_id=21"
                )
                cur.execute("DELETE FROM web.forensics_cases")
        case = store.create_case(SNAPSHOT, 7)
        self.assertIn(20, case["choices"]["attackers"])
        self.assertNotIn(21, case["choices"]["attackers"])
        self.assertTrue(
            any(c["id"] == 21 for c in case["hypotheses"]["attacker"]["candidates"])
        )

    def test_empty_plan_and_transient_server_error_preserve_untested_state(self):
        store.save_choices(self.case["id"], {"ids": [], "victims": [], "attackers": []})
        with patch.object(store, "fetch_ccp") as http:
            result = store.validate_next(self.case["id"], 7)
            self.assertTrue(result["done"])
            http.assert_not_called()
        self.assertEqual(store.list_cases()[0]["status"], "needs_input")
        store.save_choices(
            self.case["id"], {"ids": [1001], "victims": [None], "attackers": [None]}
        )
        with patch.object(store, "fetch_ccp", return_value=(502, {}, {})):
            self.reset_gate()
            result = store.validate_next(self.case["id"], 7)
        self.assertFalse(result["done"])
        self.assertEqual(store.list_cases()[0]["estimated_attempts"], 1)
        with patch.object(
            store, "fetch_ccp", return_value=(200, {}, payload(None, None))
        ):
            self.reset_gate()
            self.assertTrue(store.validate_next(self.case["id"], 7)["recovered"])

    def test_global_token_budget_and_parallel_case_lock(self):
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO web.forensics_esi_requests(cost) VALUES(3300)")
        self.reset_gate()
        request, wait = store.reserve_request()
        self.assertIsNone(request)
        self.assertGreater(wait, 0)
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_lock(%s)", (store.CASE_LOCK + self.case["id"],)
                )
                with self.assertRaises(store.ForensicsError):
                    store.validate_next(self.case["id"], 7)
                with self.assertRaises(store.ForensicsError):
                    store.save_choices(
                        self.case["id"], {"ids": [], "victims": [], "attackers": []}
                    )
                cur.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (store.CASE_LOCK + self.case["id"],),
                )


if __name__ == "__main__":
    unittest.main()
