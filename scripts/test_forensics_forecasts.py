"""Forecast, evidence filter and resumable analysis tests; only the disposable lab."""

import copy
import unittest
from datetime import timedelta
from unittest.mock import patch

from psycopg2.extras import Json

from scripts import test_forensics_reconstruction as r

store = r.store
batch = r.f.routes.forensics_batch
forecast_module = __import__(r.f.PACKAGE + ".forensics_forecast", fromlist=["*"])


class ForecastTests(unittest.TestCase):
    def test_conditional_cost_and_evidence_are_separate(self):
        h = {
            "ids": {"deduced": True, "candidates": [{"id": 100, "weight": 1}]},
            "victim": {
                "candidates": [
                    dict(id=10, weight=9, evidence={"unique_ship_local": True}),
                    dict(id=11, weight=1, evidence={}),
                ]
            },
            "attacker": {
                "candidates": [
                    dict(id=20, weight=1, evidence={"capsule_sequence": True})
                ]
            },
            "warnings": [],
        }
        choices = {"ids": [100], "victims": [10, 11], "attackers": [20]}
        plan = [(100, 10, 20, 90), (100, 11, 20, 10)]
        f = forecast_module.forecast(h, choices, plan)
        self.assertEqual(f["assessment"], "strong")
        self.assertEqual(
            (f["typical_trials"], f["upper_trials"], f["maximum_trials"]), (1, 1, 2)
        )
        self.assertEqual(f["expected_trials"], 1.1)
        # A tiny plan without pilot evidence remains weak rather than 100% certain.
        thin = copy.deepcopy(h)
        thin["victim"]["candidates"][0]["evidence"] = {}
        f = forecast_module.forecast(thin, choices, plan[:1])
        self.assertEqual(f["assessment"], "weak")
        self.assertTrue(f["conditional"])
        self.assertEqual(f["maximum_trials"], 1)

    def test_missing_ids_exhaustion_and_capped_evidence(self):
        h = {
            "ids": {"deduced": True, "candidates": []},
            "victim": {"candidates": []},
            "attacker": {"candidates": []},
            "warnings": [],
        }
        choices = {"ids": [], "victims": [None], "attackers": [None]}
        f = forecast_module.forecast(h, choices, [])
        self.assertEqual(f["assessment"], "blocked")
        self.assertIsNone(f["expected_trials"])
        choices["ids"] = [100]
        self.assertEqual(
            forecast_module.forecast(h, choices, [])["assessment"], "exhausted"
        )
        self.assertEqual(
            forecast_module.forecast(h, choices, [], recovered=True)["assessment"],
            "confirmed",
        )
        self.assertEqual(
            forecast_module.forecast(h, choices, [], analysis_error="Timeout")[
                "assessment"
            ],
            "blocked",
        )

    def test_date_scope_rejects_inverted_bounds(self):
        self.assertEqual(batch.date_scope(), [None, None])
        with self.assertRaises(ValueError):
            batch.date_scope("2026-08-02", "2026-08-01")


class AnalysisEndpointTests(unittest.TestCase):
    setUp = r.f.EndpointTests.setUp

    def test_analysis_requires_dev_permission_and_setup(self):
        with patch.object(store, "ready", return_value=False):
            self.assertEqual(
                self.client.get("/admin/killmail-forensics/analysis").status_code, 503
            )
        self.user = {"permissions": set()}
        self.assertEqual(
            self.client.get("/admin/killmail-forensics/analysis").status_code, 403
        )
        self.assertEqual(
            self.client.post(
                "/admin/killmail-forensics/analysis/stop", json={}
            ).status_code,
            403,
        )

    def test_readiness_failure_returns_json_without_database_details(self):
        with patch.object(store, "ready", side_effect=RuntimeError("private database details")):
            response = self.client.get("/admin/killmail-forensics/analysis")
        self.assertEqual(response.status_code, 500)
        self.assertIn("error", response.json())
        self.assertNotIn("private database", response.text)

    def test_batch_launch_requires_jobs_run_and_same_origin(self):
        with patch.object(store, "ready", return_value=True):
            self.assertEqual(
                self.client.post(
                    "/admin/killmail-forensics/analysis/start", json={}
                ).status_code,
                403,
            )
            self.user["permissions"].add("admin.jobs.run")
            self.assertEqual(
                self.client.post(
                    "/admin/killmail-forensics/analysis/start",
                    json={},
                    headers={"Origin": "https://evil.example"},
                ).status_code,
                403,
            )

    def test_recovery_launches_only_neighbor_maintenance_and_keeps_result_on_launch_error(
        self,
    ):
        import sys, types

        jobs = types.ModuleType(r.f.PACKAGE + ".jobs")
        from unittest.mock import Mock

        jobs.run_forensics_analysis_job = Mock(
            side_effect=RuntimeError("already running")
        )
        with (
            patch.dict(sys.modules, {jobs.__name__: jobs}),
            patch.object(store, "ready", return_value=True),
            patch.object(
                store,
                "validate_next",
                return_value={"done": True, "recovered": True, "neighbors_queued": 1},
            ),
        ):
            response = self.client.post(
                "/admin/killmail-forensics/cases/1/validate", json={}
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["recovered"])
        jobs.run_forensics_analysis_job.assert_called_once_with(7, refresh_only=True)

    def test_job_registration_and_scoped_launch_are_passive(self):
        import sys

        config = sys.modules[r.f.PACKAGE + ".config"]
        with patch.object(
            config,
            "JOBS_CONFIG_PATH",
            r.f.ROOT / "config/offline-jobs.json",
            create=True,
        ):
            jobs = r.f.load("jobs")
        with (
            patch.object(jobs, "_read_config_file", return_value={"jobs": []}),
            patch.object(
                jobs, "_start_process", return_value=(True, "started")
            ) as start,
        ):
            definition = jobs.get_job("analyze_hidden_killmails")
            self.assertEqual(definition["type"], "killmail_forensics_analysis")
            start.assert_not_called()
            jobs.run_forensics_analysis_job(7, "2026-08-01", "2026-08-02")
            command = start.call_args.args[1]
            self.assertEqual(
                command[1], str(r.f.ROOT / "scripts/analyze_hidden_killmails.py")
            )
            self.assertEqual(
                command[2:],
                [
                    "--user-id",
                    "7",
                    "--date-from",
                    "2026-08-01",
                    "--date-to",
                    "2026-08-02",
                ],
            )
            jobs.run_forensics_analysis_job(7, refresh_only=True)
            self.assertIn("--refresh-only", start.call_args.args[1])
            with self.assertRaises(jobs.JobError):
                jobs.run_forensics_analysis_job(7, "2026-08-03", "2026-08-01")


@unittest.skipUnless(
    r.SOCKET.startswith("/tmp/"), "Requires the disposable PostgreSQL lab"
)
class AnalysisTests(unittest.TestCase):
    connect = r.PersistenceTests.connect
    setUp = r.PersistenceTests.setUp

    def test_evidence_filters_match_one_candidate_and_requested_role(self):
        info = copy.deepcopy(self.case["forecast"])
        info["context_flags"] = ["deduced_id"]
        info["candidate_evidence"] = {
            "victim": [
                {"id": 10, "flags": ["same_system"]},
                {"id": 11, "flags": ["recent_ship"]},
            ],
            "attacker": [{"id": 20, "flags": ["same_system", "recent_ship"]}],
        }
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE web.forensics_cases SET forecast=%s WHERE id=%s",
                    (Json(info), self.case["id"]),
                )
        self.assertEqual(
            store.list_cases(
                evidence=["same_system", "recent_ship"], evidence_role="victim"
            ),
            [],
        )
        self.assertEqual(
            len(
                store.list_cases(
                    evidence=["same_system", "recent_ship"], evidence_role="attacker"
                )
            ),
            1,
        )
        self.assertEqual(
            len(
                store.list_cases(evidence=["deduced_id", "same_system", "recent_ship"])
            ),
            1,
        )
        self.assertEqual(store.list_cases(evidence=["object_victim"]), [])
        with self.assertRaises(ValueError):
            store.list_cases(evidence=["arbitrary-sql"])

    def test_forecast_recomputed_after_invalid_trial_and_manual_choices(self):
        store.save_choices(
            self.case["id"], {"ids": [1001], "victims": [10], "attackers": [20, 21]}
        )
        before = store.list_cases("estimate")[0]
        with patch.object(store, "fetch_ccp", return_value=(404, {}, {})):
            store.validate_next(self.case["id"], 7)
        after = store.list_cases("estimate")[0]
        self.assertEqual(
            (before["forecast"]["maximum_trials"], after["forecast"]["maximum_trials"]),
            (2, 1),
        )
        self.assertEqual(after["forecast"]["typical_trials"], 1)
        store.save_choices(
            self.case["id"],
            {"ids": [1001], "victims": [10, 999], "attackers": [20, 21]},
        )
        self.assertTrue(store.list_cases()[0]["choices_customized"])
        self.assertEqual(store.list_cases()[0]["forecast"]["maximum_trials"], 3)

    def test_batch_stop_resume_and_failure_retry_never_call_ccp(self):
        with self.connect() as conn:
            with conn.cursor() as cur:
                for row in (4, 5, 6):
                    cur.execute(
                        "INSERT INTO mer.killmails VALUES('2026-08-01',%s,%s,587,588,100,200,30000142,NULL,false)",
                        (row, r.AT + timedelta(seconds=row)),
                    )
                cur.execute(
                    "UPDATE mer.killmails SET victim_ship_type_id=NULL WHERE source_row=4"
                )
        calls = 0
        original = batch._analyze_reference

        def analyze(*args):
            nonlocal calls
            calls += 1
            return original(*args)

        with (
            patch.object(store, "fetch_ccp") as ccp,
            patch.object(batch, "_analyze_reference", side_effect=analyze),
        ):
            first = batch.run_analysis(7, should_stop=lambda: calls >= 1)
            self.assertEqual(first["status"], "stopped")
            self.assertEqual(first["analyzed"], 1)
            with self.assertLogs(batch.logger, level="ERROR"):
                second = batch.run_analysis(7)
            self.assertEqual((second["analyzed"], second["failed"]), (1, 1))
            blocked = store.list_cases(assessment="blocked")
            self.assertEqual(len(blocked), 1)
            self.assertTrue(blocked[0]["analysis_error"])
            with self.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE mer.killmails SET victim_ship_type_id=587 WHERE source_row=4"
                    )
            third = batch.run_analysis(7)
            self.assertEqual((third["analyzed"], third["failed"]), (1, 0))
            self.assertEqual(len(store.list_cases()), 4)
            self.assertEqual(batch.run_analysis(7)["analyzed"], 0)
            ccp.assert_not_called()

    def test_queue_failure_cannot_undo_a_confirmed_kill(self):
        store.save_choices(
            self.case["id"], {"ids": [1001], "victims": [10], "attackers": [20]}
        )
        original = store.execute

        def execute(conn, sql, *args, **kwargs):
            if "INSERT INTO web.forensics_refresh_queue" in sql:
                raise RuntimeError("Queue temporarily unavailable")
            return original(conn, sql, *args, **kwargs)

        with (
            patch.object(store, "execute", side_effect=execute),
            patch.object(store, "fetch_ccp", return_value=(200, {}, r.payload(10, 20))),
            self.assertLogs(store.logger, level="ERROR"),
        ):
            result = store.validate_next(self.case["id"], 7)
        self.assertTrue(result["recovered"])
        self.assertTrue(result["neighbor_refresh_error"])
        self.assertEqual(
            store.recovered_detail(1001)["payload"]["victim"]["character_id"], 10
        )
        self.assertEqual(store.list_cases(recovered=True)[0]["status"], "recovered")

    def test_global_job_drains_more_than_one_refresh_batch(self):
        with self.connect() as conn:
            with conn.cursor() as cur:
                for number in range(55):
                    cur.execute(
                        """INSERT INTO web.forensics_cases(kill_datetime,source_month,source_row,snapshot,hypotheses,choices)
                        VALUES(%s,'2026-08-01',%s,%s,%s,%s) RETURNING id""",
                        (
                            r.AT + timedelta(seconds=number + 100),
                            number + 100,
                            Json(r.SNAPSHOT),
                            Json(self.case["hypotheses"]),
                            Json(self.case["choices"]),
                        ),
                    )
                    case_id = cur.fetchone()[0]
                    cur.execute(
                        "INSERT INTO web.forensics_refresh_queue(case_id,trigger_killmail_id) VALUES(%s,1001)",
                        (case_id,),
                    )
        with (
            patch.object(batch, "_analyze_reference") as analyze,
            patch.object(store, "fetch_ccp") as ccp,
        ):
            batch.run_analysis(7)
            self.assertEqual(analyze.call_count, 55)
            ccp.assert_not_called()
        state = batch.analysis_status()
        self.assertEqual(state["pending_refresh"], 0)
        self.assertEqual(state["run"]["refreshed"], 55)

    def _recover_and_refresh(self, manual=False):
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE mer.killmails SET resolved_km=NULL WHERE source_row=1"
                )
                cur.execute(
                    "UPDATE mer.killmails SET kill_datetime=%s,resolved_km=ARRAY[1003]::bigint[] WHERE source_row=3",
                    (r.AT + timedelta(seconds=30),),
                )
                cur.execute(
                    "INSERT INTO mer.killmails VALUES('2026-08-01',4,%s,587,588,100,200,30000142,NULL,false)",
                    (r.AT + timedelta(seconds=15),),
                )
        neighbor = store.create_case(
            dict(
                r.SNAPSHOT,
                source_row=4,
                kill_datetime=(r.AT + timedelta(seconds=15)).isoformat(),
            ),
            7,
        )
        self.assertEqual(neighbor["choices"]["ids"], [])
        self.assertFalse(
            any(c["id"] == 99 for c in neighbor["hypotheses"]["victim"]["candidates"])
        )
        if manual:
            store.save_choices(
                neighbor["id"], {"ids": [7777], "victims": [11], "attackers": [21]}
            )
        store.save_choices(
            self.case["id"], {"ids": [1001], "victims": [99], "attackers": [98]}
        )
        with patch.object(
            store, "fetch_ccp", return_value=(200, {}, r.payload(99, 98))
        ):
            result = store.validate_next(self.case["id"], 7)
        self.assertTrue(result["recovered"])
        self.assertEqual(result["neighbors_queued"], 1)
        self.assertEqual(batch.analysis_status()["pending_refresh"], 1)
        with patch.object(store, "fetch_ccp") as ccp:
            batch.run_analysis(7, refresh_only=True)
            ccp.assert_not_called()
        updated = next(c for c in store.list_cases() if c["id"] == neighbor["id"])
        self.assertTrue(updated["hypotheses"]["ids"]["deduced"])
        self.assertTrue(
            any(
                c["id"] == 99 and c["evidence"]["recovered_neighbor"]
                for c in updated["hypotheses"]["victim"]["candidates"]
            )
        )
        self.assertEqual(batch.analysis_status()["pending_refresh"], 0)
        self.assertEqual(batch.analysis_status()["run"]["refreshed"], 1)
        return updated

    def test_recovery_supplies_new_anchor_and_new_pilots_to_neighbors(self):
        updated = self._recover_and_refresh()
        self.assertEqual(updated["choices"]["ids"], [1002])
        self.assertIn(99, updated["choices"]["victims"])
        self.assertIn(98, updated["choices"]["attackers"])
        self.assertFalse(updated["choices_customized"])
        self.assertEqual(len(store.list_cases(evidence=["recovered_neighbor"])), 1)

    def test_neighbor_refresh_preserves_explicit_manual_plan(self):
        updated = self._recover_and_refresh(manual=True)
        self.assertEqual(
            updated["choices"], {"ids": [7777], "victims": [11], "attackers": [21]}
        )
        self.assertTrue(updated["choices_customized"])


if __name__ == "__main__":
    unittest.main()
