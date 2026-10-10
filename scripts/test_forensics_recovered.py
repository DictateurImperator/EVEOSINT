"""Recovered filters, manual zKill publishing and CCP credits in the disposable lab."""

import unittest
from unittest.mock import patch
from psycopg2.extras import Json
from scripts import test_forensics_reconstruction as r

recovered = r.f.routes.recovered_service
store = r.store


@unittest.skipUnless(r.SOCKET.startswith("/tmp/"), "Requires disposable PostgreSQL lab")
class RecoveredTests(unittest.TestCase):
    connect = r.PersistenceTests.connect
    setUp = r.PersistenceTests.setUp

    def confirm(self):
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO web.forensics_recovered(killmail_id,hash,case_id,payload) VALUES(1001,%s,%s,%s)",
                    ("a" * 40, self.case["id"], Json(r.payload())),
                )
                cur.execute(
                    "UPDATE web.forensics_cases SET status='recovered',recovered_killmail_id=1001,snapshot=snapshot||%s WHERE id=%s",
                    (Json({"ccp_isk_lost": 123456789}), self.case["id"]),
                )
                cur.execute(
                    "INSERT INTO web.forensics_attempts(case_id,killmail_id,hash,score,outcome) VALUES(%s,1001,%s,1,'recovered')",
                    (self.case["id"], "a" * 40),
                )

    def test_filters_use_confirmed_victim_and_all_attackers(self):
        self.confirm()
        rows = recovered.page()["kills"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["hash_tests"], 1)
        self.assertEqual(rows[0]["mer_value"], "123.46m")
        self.assertEqual(rows[0]["display"]["victim_character"]["name"], "Pilot 10")
        self.assertEqual(
            len(
                recovered.page({"builder_entity_include": ["victim:character:10"]})[
                    "kills"
                ]
            ),
            1,
        )
        self.assertEqual(
            len(
                recovered.page({"builder_entity_include": ["attacker:character:10"]})[
                    "kills"
                ]
            ),
            0,
        )
        self.assertEqual(
            len(recovered.page({"builder_ship_include": ["attacker:588"]})["kills"]), 1
        )
        self.assertEqual(
            len(recovered.page({"builder_ship_exclude": ["victim:587"]})["kills"]), 0
        )
        self.assertEqual(len(recovered.page({"date_from": "2026-08-02"})["kills"]), 0)
        with self.connect() as conn:
            with conn.cursor() as cur:
                p = r.payload()
                p["attackers"].append(
                    {
                        "character_id": 99,
                        "ship_type_id": 999,
                        "corporation_id": 900,
                        "final_blow": False,
                    }
                )
                cur.execute("UPDATE web.forensics_recovered SET payload=%s", (Json(p),))
        self.assertEqual(
            len(
                recovered.page({"builder_entity_include": ["attacker:character:99"]})[
                    "kills"
                ]
            ),
            1,
        )
        self.assertEqual(
            len(recovered.page({"builder_ship_include": ["attacker:999"]})["kills"]), 1
        )

    def test_alliance_and_zone_filters_and_missing_npc_fields(self):
        self.confirm()
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE TABLE entities.corporation_alliance_history(corporation_id bigint,alliance_id bigint,start_date timestamptz,end_date timestamptz)"
                )
                payload = r.payload()
                payload["victim"]["alliance_id"] = 300
                payload["attackers"][0]["alliance_id"] = 400
                cur.execute(
                    "UPDATE web.forensics_recovered SET payload=%s", (Json(payload),)
                )
        self.assertEqual(
            len(
                recovered.page({"builder_entity_include": ["victim:alliance:300"]})[
                    "kills"
                ]
            ),
            1,
        )
        self.assertEqual(
            len(
                recovered.page({"builder_entity_include": ["attacker:alliance:400"]})[
                    "kills"
                ]
            ),
            1,
        )
        with patch.object(
            r.f.entities, "_killmail_builder_zone_system_ids", return_value=[30000142]
        ):
            self.assertEqual(
                len(
                    recovered.page({"builder_zone_include": ["system:30000142"]})[
                        "kills"
                    ]
                ),
                1,
            )
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE web.forensics_recovered SET payload=%s",
                    (Json(r.payload(victim=None, attacker=None)),),
                )
        self.assertEqual(
            len(
                recovered.page({"builder_entity_exclude": ["both:character:10"]})[
                    "kills"
                ]
            ),
            1,
        )
        self.assertEqual(
            len(
                recovered.page({"builder_entity_exclude": ["both:alliance:300"]})[
                    "kills"
                ]
            ),
            1,
        )

    def test_submission_is_confirmed_only_and_accepted_is_not_resent(self):
        with patch.object(recovered, "post_zkill") as post:
            with self.assertRaises(ValueError):
                recovered.submit(1001, 7)
            post.assert_not_called()
        self.confirm()
        with patch.object(
            recovered,
            "post_zkill",
            return_value=(200, {}, {"status": "success", "new": True}),
        ) as post:
            self.assertTrue(recovered.submit(1001, 7)["accepted"])
            self.assertTrue(recovered.submit(1001, 7)["accepted"])
            post.assert_called_once_with(1001, "a" * 40)
        self.assertEqual(len(recovered.page(publication="accepted")["kills"]), 1)
        self.assertEqual(len(recovered.page(publication="pending")["kills"]), 0)

    def test_failed_or_uncertain_response_never_counts_as_accepted(self):
        self.confirm()
        for status, payload, state in (
            (200, {"status": "error"}, "failed"),
            (429, {}, "failed"),
            (408, {}, "uncertain"),
            (0, {}, "uncertain"),
        ):
            with self.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM web.forensics_zkill_submissions")
            with patch.object(
                recovered, "post_zkill", return_value=(status, {}, payload)
            ) as post:
                result = recovered.submit(1001, 7)
                self.assertFalse(result["accepted"])
                self.assertGreaterEqual(result["retry_after"], 120)
                recovered.submit(1001, 7)
                post.assert_called_once()
            self.assertEqual(len(recovered.page(publication=state)["kills"]), 1)

    def test_credits_refund_expired_calls_and_show_actual_ccp_headers(self):
        request_id, wait = store.reserve_request()
        self.assertEqual(wait, 0)
        self.assertEqual(store.request_budget()["used"], 5)
        store.finish_request(
            request_id,
            200,
            {
                "X-Ratelimit-Remaining": "3598",
                "X-Ratelimit-Limit": "3600/15m",
                "X-Ratelimit-Used": "2",
            },
        )
        budget = store.request_budget()
        self.assertEqual(budget["remaining"], 3298)
        self.assertEqual(budget["reported"]["remaining"], 3598)
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE web.forensics_esi_requests SET reserved_at=NOW()-INTERVAL '16 minutes'"
                )
        self.assertEqual(store.request_budget()["remaining"], 3300)

    def test_missing_publishing_migration_keeps_recovered_list_available(self):
        self.confirm()
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DROP TABLE web.forensics_zkill_submissions")
        result = recovered.page()
        self.assertFalse(result["publishing_ready"])
        self.assertEqual(len(result["kills"]), 1)
        with patch.object(recovered, "post_zkill") as post:
            with self.assertRaises(ValueError):
                recovered.submit(1001, 7)
            post.assert_not_called()


class RecoveredEndpointTests(unittest.TestCase):
    setUp = r.f.EndpointTests.setUp

    def test_submit_requires_permission_same_origin_and_json(self):
        with (
            patch.object(store, "ready", return_value=True),
            patch.object(recovered, "submit") as submit,
        ):
            response = self.client.post(
                "/admin/killmail-forensics/recovered/1001/submit",
                json={},
                headers={"Origin": "https://evil.example"},
            )
            self.assertEqual(response.status_code, 403)
            response = self.client.post(
                "/admin/killmail-forensics/recovered/1001/submit"
            )
            self.assertEqual(response.status_code, 415)
            self.user["permissions"] = set()
            response = self.client.post(
                "/admin/killmail-forensics/recovered/1001/submit", json={}
            )
            self.assertEqual(response.status_code, 403)
            submit.assert_not_called()

    def test_data_route_renders_confirmed_kill_and_trial_count(self):
        result = {"kills": [], "has_next": False, "publishing_ready": True}
        with (
            patch.object(store, "ready", return_value=True),
            patch.object(recovered, "page", return_value=result),
        ):
            response = self.client.get("/admin/killmail-forensics/recovered/data")
        self.assertEqual(response.status_code, 200)
        self.assertIn("No recovered killmails", response.json()["html"])
