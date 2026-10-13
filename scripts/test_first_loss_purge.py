"""Purge safety tests. Integration runs only in an explicitly isolated test database."""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

import psycopg2
from scripts.purge_first_loss_study import LOCK, TABLES, purge
from scripts.test_first_loss_admin import jobs, routes
from fastapi import FastAPI
from fastapi.testclient import TestClient


class ControlTests(unittest.TestCase):
    def test_manual_job_and_mutual_exclusion(self):
        with patch.object(
            jobs, "_read_config_file", return_value={"jobs": []}
        ), patch.object(
            jobs, "_cleanup_stale_pid", return_value=(False, None)
        ), patch.object(
            jobs, "_start_process"
        ) as launch:
            job = jobs.get_job("purge_first_loss_study")
            launch.assert_not_called()
            jobs.run_first_loss_purge()
            launch.assert_called_once_with(job, job["command"])
        with patch.object(
            jobs, "_cleanup_stale_pid", return_value=(True, 123)
        ), patch.object(jobs, "_start_process") as launch:
            for action in (jobs.run_first_loss_purge, jobs.run_first_loss_study):
                with self.assertRaises(jobs.JobError):
                    action()
            launch.assert_not_called()

    def test_report_urls_and_purge_permissions(self):
        app = FastAPI()
        app.include_router(routes.router)
        client = TestClient(app)
        user = {"id": 1, "username": "admin", "permissions": {"admin.jobs.view"}}
        with patch.object(routes, "require_login", return_value=user), patch.object(
            routes, "run_first_loss_purge"
        ) as launch, patch.object(routes, "audit_log") as audit, patch.object(
            routes, "report", return_value=None
        ), patch.object(
            routes, "first_loss_progress", return_value={"running": False}
        ), patch.object(
            routes,
            "first_loss_purge_progress",
            return_value={"running": False, "progress": None},
        ):
            for url in (
                "/admin/report/first-loss/status",
                "/admin/debug/first-loss/status",
            ):
                self.assertEqual(client.get(url).status_code, 200)
            self.assertEqual(
                client.post("/admin/report/first-loss/purge").status_code, 403
            )
            launch.assert_not_called()
            user["permissions"].add("admin.jobs.run")
            self.assertEqual(
                client.post("/admin/report/first-loss/purge").json(), {"started": True}
            )
            launch.assert_called_once()
            audit.assert_called_once()
            launch.side_effect = jobs.JobError("Study is running")
            self.assertEqual(
                client.post("/admin/report/first-loss/purge").status_code, 409
            )


@unittest.skipUnless(
    os.environ.get("FIRST_LOSS_PURGE_TEST_DSN"), "Isolated database required"
)
class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg2.connect(os.environ["FIRST_LOSS_PURGE_TEST_DSN"])
        # Never let these destructive fixtures run on the source/application database.
        if self.conn.info.dbname != "first_loss_purge_test":
            self.conn.close()
            raise RuntimeError("Use a dedicated first_loss_purge_test database")
        with self.conn.cursor() as cur:
            cur.execute(
                "DROP SCHEMA IF EXISTS research CASCADE; CREATE SCHEMA research"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS public.source_killmails(id bigint PRIMARY KEY)"
            )
            cur.execute(
                "INSERT INTO public.source_killmails VALUES(42) ON CONFLICT DO NOTHING"
            )
            cur.execute(
                "CREATE TABLE research.other_study(id int); INSERT INTO research.other_study VALUES(1)"
            )
            cur.execute(
                "CREATE TABLE research.first_loss_runs(id int PRIMARY KEY, payload text)"
            )
            for table in TABLES[:-1]:
                cur.execute(
                    f"CREATE TABLE research.{table}(run_id int REFERENCES research.first_loss_runs, payload text)"
                )
            cur.execute(
                "INSERT INTO research.first_loss_runs VALUES(1, 'saved result')"
            )
            for table in TABLES[:-1]:
                cur.execute(
                    f"INSERT INTO research.{table} SELECT 1, repeat(md5(i::text),20) FROM generate_series(1,2000) i"
                )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_purge_frees_storage_preserves_sources_and_is_repeatable(self):
        callbacks = []
        result = purge(self.conn, lambda: callbacks.append(True))
        self.assertEqual(result["tables"], 4)
        self.assertEqual(callbacks, [True])
        with self.conn.cursor() as cur:
            total = 0
            for table in TABLES:
                cur.execute(f"SELECT count(*) FROM research.{table}")
                self.assertEqual(cur.fetchone()[0], 0)
                cur.execute(
                    "SELECT pg_total_relation_size(%s::regclass)",
                    ("research." + table,),
                )
                total += cur.fetchone()[0]
            self.assertLess(total, result["previous_bytes"])
            cur.execute("SELECT id FROM public.source_killmails")
            self.assertEqual(cur.fetchone()[0], 42)
            cur.execute("SELECT id FROM research.other_study")
            self.assertEqual(cur.fetchone()[0], 1)
        self.conn.commit()
        self.assertEqual(purge(self.conn)["tables"], 4)

    def test_live_study_lock_prevents_any_deletion(self):
        other = psycopg2.connect(os.environ["FIRST_LOSS_PURGE_TEST_DSN"])
        try:
            with other.cursor() as cur:
                cur.execute("SELECT pg_advisory_lock(%s)", (LOCK,))
            with self.assertRaisesRegex(RuntimeError, "running"):
                purge(self.conn)
            with self.conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM research.first_loss_runs")
                self.assertEqual(cur.fetchone()[0], 1)
        finally:
            other.close()

    def test_unexpected_foreign_key_refuses_cascade_and_rolls_back(self):
        with self.conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE research.external_result(run_id int REFERENCES research.first_loss_runs)"
            )
            cur.execute("INSERT INTO research.external_result VALUES(1)")
        self.conn.commit()
        with self.assertRaises(psycopg2.Error):
            purge(self.conn)
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM research.first_loss_players")
            self.assertEqual(cur.fetchone()[0], 2000)
            cur.execute("SELECT count(*) FROM research.external_result")
            self.assertEqual(cur.fetchone()[0], 1)

    def test_missing_tables_are_safe(self):
        with self.conn.cursor() as cur:
            for table in TABLES:
                cur.execute(f"DROP TABLE research.{table}")
        self.conn.commit()
        self.assertEqual(purge(self.conn), {"tables": 0, "previous_bytes": 0})


if __name__ == "__main__":
    unittest.main()
