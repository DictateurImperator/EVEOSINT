"""Offline API/permission tests and isolated transactional price import checks."""

import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime, UTC
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from scripts import import_zkill_prices as importer
from scripts.test_first_loss_admin import jobs
from scripts import test_killmail_forensics as fixture

routes = fixture.load("routes_admin_prices")


def response(payload, code=200, headers=None):
    r = requests.Response()
    r.status_code = code
    r._content = json.dumps(payload).encode()
    r._content_consumed = True
    r.headers.update(headers or {})
    return r


class PriceTests(unittest.TestCase):
    def test_mixed_numbers_keep_decimal_precision(self):
        data = importer.normalize_prices(
            {"34": 3.93, "587": 278000, "11567": "127788199885.32", "99": 0}
        )
        self.assertEqual(data["11567"], Decimal("127788199885.32"))
        encoded = json.loads(importer.prices_json(data), parse_float=Decimal)
        self.assertEqual(encoded["11567"], data["11567"])
        self.assertEqual(encoded["99"], 0)
        self.assertNotIn("missing", encoded)
        self.assertEqual(importer.normalize_prices([]), {})
        for payload in (
            {"error": "retry later"},
            [1],
            {"34": None},
            {"34": True},
            {"34": "NaN"},
            {"34": -1},
            {"34": "Infinity"},
            {"34": "1e999"},
            {"34": "1e-999"},
            {"currentPrice": 1},
            {"34": {}},
            {"034": 1, "34": 2},
        ):
            with self.assertRaises(ValueError):
                importer.normalize_prices(payload)

    def test_user_agent_gzip_retry_after_and_rate_limit(self):
        session = MagicMock()
        limited = response({}, 429, {"Retry-After": "120"})
        session.get.side_effect = [limited, response({"34": "3.93"})]
        client = importer.Client(session, threading.Event())
        with patch.object(client, "wait") as wait, patch.object(
            importer.time, "monotonic", return_value=10
        ):
            values, url, checksum = client.fetch(date(2026, 10, 11))
            self.assertEqual(values["34"], Decimal("3.93"))
            self.assertEqual(wait.call_args_list[1].args[0], 120)
        self.assertTrue(url.endswith("/date/2026-10-11/"))
        self.assertEqual(len(checksum), 64)
        session.headers.update.assert_called_once()
        self.assertIn(
            "gzip", session.headers.update.call_args.args[0]["Accept-Encoding"]
        )
        self.assertIn(
            "EVEOSINT", session.headers.update.call_args.args[0]["User-Agent"]
        )
        session.get.side_effect = None
        session.get.return_value = response({}, 429)
        client = importer.Client(session, threading.Event(), attempts=2)
        with patch.object(client, "wait"), self.assertRaises(importer.RateLimited):
            client.fetch(date(2026, 10, 11))

    def test_stop_interrupts_wait_without_a_request(self):
        event = threading.Event()
        event.set()
        session = MagicMock()
        with self.assertRaises(importer.StopRequested):
            importer.Client(session, event).fetch(date(2020, 1, 1))
        session.get.assert_not_called()
        self.assertEqual(
            importer.retry_after(
                "Tue, 13 Oct 2026 00:02:00 GMT", datetime(2026, 10, 13, tzinfo=UTC)
            ),
            120,
        )

    def test_job_dates_and_process_identity(self):
        with patch.object(
            jobs, "_read_config_file", return_value={"jobs": []}
        ), patch.object(jobs, "_start_process") as launch:
            job = jobs.get_job("import_zkill_prices")
            launch.assert_not_called()
            jobs.run_zkill_prices("2003-01-01", "2003-01-02")
            launch.assert_called_once_with(
                job,
                job["command"] + ["--from", "2003-01-01", "--through", "2003-01-02"],
            )
            for start, end in (
                ("invalid", ""),
                ("2002-01-01", "2003-01-01"),
                ("2020-01-02", "2020-01-01"),
                ("", "2999-01-01"),
            ):
                with self.assertRaises(jobs.JobError):
                    jobs.run_zkill_prices(start, end)
        with patch.object(jobs, "get_job", return_value=job), patch.object(
            jobs, "_cleanup_stale_pid", return_value=(True, 123)
        ), patch.object(
            Path, "read_bytes", return_value=b"python\0/unrelated.py\0"
        ), patch.object(
            jobs.os, "kill"
        ) as kill:
            with self.assertRaises(jobs.JobError):
                jobs.stop_zkill_prices()
            kill.assert_not_called()

    def test_view_cannot_start_or_stop(self):
        app = FastAPI()
        app.include_router(routes.router)
        client = TestClient(app)
        user = {"id": 1, "username": "admin", "permissions": {"admin.jobs.view"}}
        with patch.object(routes, "require_login", return_value=user), patch.object(
            routes, "run_zkill_prices"
        ) as launch, patch.object(routes, "stop_zkill_prices") as stop, patch.object(
            routes, "summary", return_value=None
        ), patch.object(
            routes,
            "zkill_prices_progress",
            return_value={"running": False, "progress": None},
        ), patch.object(
            routes, "audit_log"
        ):
            self.assertEqual(client.get("/admin/prices/status").status_code, 200)
            self.assertEqual(client.post("/admin/prices/start").status_code, 403)
            self.assertEqual(client.post("/admin/prices/stop").status_code, 403)
            launch.assert_not_called()
            stop.assert_not_called()
            user["permissions"].add("admin.jobs.run")
            self.assertEqual(
                client.post("/admin/prices/start").json(), {"started": True}
            )
            launch.assert_called_once_with("", "")
            self.assertEqual(
                client.post("/admin/prices/stop").json(), {"stop_requested": True}
            )
            stop.assert_called_once()


@unittest.skipUnless(
    os.environ.get("ZKILL_PRICES_TEST_DSN"), "Isolated PostgreSQL required"
)
class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg2.connect(os.environ["ZKILL_PRICES_TEST_DSN"])
        if self.conn.info.dbname != "zkill_prices_test":
            self.conn.close()
            raise RuntimeError("Use an isolated zkill_prices_test database")
        with self.conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS market CASCADE")
            cur.execute(importer.SCHEMA.read_text())
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_upsert_complete_day_and_invalid_payload_keeps_previous(self):
        client = MagicMock()
        client.fetch.return_value = (
            {"34": Decimal("3.93"), "11567": Decimal("127788199885.32")},
            "https://zkillboard.com/api/prices/date/2026-10-11/",
            "a" * 64,
        )
        day = date(2026, 10, 11)
        self.assertEqual(importer.import_day(self.conn, client, day), 2)
        self.assertEqual(importer.import_day(self.conn, client, day), 2)
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM market.zkill_daily_prices")
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute(
                "SELECT (prices->>'11567')::numeric, prices ? '99999999' FROM market.zkill_daily_prices"
            )
            self.assertEqual(cur.fetchone(), (Decimal("127788199885.32"), False))
        self.conn.commit()
        client.fetch.side_effect = ValueError("invalid API response")
        with self.assertRaises(ValueError):
            importer.import_day(self.conn, client, day)
        with self.conn.cursor() as cur:
            cur.execute("SELECT item_count FROM market.zkill_daily_prices")
            self.assertEqual(cur.fetchone()[0], 2)
        self.conn.commit()

    def test_main_resumes_skips_saved_days_and_records_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = {
                "db_name": "zkill_prices_test",
                "db_user": "codex",
                "db_password": "",
                "db_host": self.conn.info.host,
                "db_port": self.conn.info.port,
            }
            (root / "db.json").write_text(json.dumps(cfg))
            argv = [
                "prices",
                "--db-config",
                str(root / "db.json"),
                "--progress",
                str(root / "progress.json"),
                "--from",
                "2003-01-01",
                "--through",
                "2003-01-02",
                "--min-free-gb",
                "0",
            ]
            with patch.object(sys, "argv", argv), patch.object(
                importer.Client,
                "fetch",
                return_value=(
                    {},
                    "https://zkillboard.com/api/prices/date/2003-01-01/",
                    "b" * 64,
                ),
            ) as fetch:
                self.assertEqual(importer.main(), 0)
                self.assertEqual(fetch.call_count, 2)
                fetch.reset_mock()
                self.assertEqual(importer.main(), 0)
                fetch.assert_not_called()
            state = json.loads((root / "progress.json").read_text())
            self.assertEqual(state["skipped"], 2)
            self.assertEqual(state["phase"], "completed")
            with self.conn.cursor() as cur:
                cur.execute("SELECT status, item_count FROM market.zkill_daily_prices")
                self.assertEqual(cur.fetchall(), [("empty", 0), ("empty", 0)])

    def cli_config(self, root):
        cfg = {
            "db_name": "zkill_prices_test",
            "db_user": "codex",
            "db_password": "",
            "db_host": self.conn.info.host,
            "db_port": self.conn.info.port,
        }
        (root / "db.json").write_text(json.dumps(cfg))
        return [
            "prices",
            "--db-config",
            str(root / "db.json"),
            "--progress",
            str(root / "progress.json"),
            "--from",
            "2003-01-01",
            "--through",
            "2003-01-02",
            "--min-free-gb",
            "0",
        ]

    def test_failed_day_is_retried_without_redownloading_the_good_day(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(sys, "argv", self.cli_config(root)), patch.object(
                importer.Client, "fetch"
            ) as fetch:
                fetch.side_effect = [
                    ValueError("API returned HTML"),
                    ({"34": Decimal("3.93")}, "url", "c" * 64),
                ]
                self.assertEqual(importer.main(), 1)
                fetch.reset_mock()
                fetch.side_effect = None
                fetch.return_value = ({"34": Decimal("3.94")}, "url", "d" * 64)
                self.assertEqual(importer.main(), 0)
                fetch.assert_called_once_with(date(2003, 1, 1))
            state = json.loads((root / "progress.json").read_text())
            self.assertEqual(state["skipped"], 1)
            self.assertEqual(state["downloaded"], 1)

    def test_advisory_lock_refuses_duplicate_and_preserves_live_progress(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (importer.LOCK,))
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                argv = self.cli_config(root)
                (root / "progress.json").write_text("live importer status")
                with patch.object(sys, "argv", argv), patch.object(
                    importer.Client, "fetch"
                ) as fetch:
                    self.assertEqual(importer.main(), 1)
                    fetch.assert_not_called()
                self.assertEqual(
                    (root / "progress.json").read_text(), "live importer status"
                )
        finally:
            with self.conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (importer.LOCK,))
            self.conn.commit()

    def test_recent_snapshot_refresh_replaces_instead_of_appending(self):
        from datetime import timedelta

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            argv = self.cli_config(root)
            day = datetime.now(UTC).date() - timedelta(days=1)
            argv[argv.index("--from") + 1] = day.isoformat()
            argv[argv.index("--through") + 1] = day.isoformat()
            with patch.object(sys, "argv", argv), patch.object(
                importer.Client, "fetch"
            ) as fetch:
                fetch.return_value = ({"34": Decimal("3.93")}, "url", "c" * 64)
                self.assertEqual(importer.main(), 0)
                fetch.return_value = ({"34": Decimal("4.00")}, "url", "d" * 64)
                self.assertEqual(importer.main(), 0)
                self.assertEqual(fetch.call_count, 2)
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*),max((prices->>'34')::numeric) FROM market.zkill_daily_prices"
                )
                self.assertEqual(cur.fetchone(), (1, Decimal("4.00")))


if __name__ == "__main__":
    unittest.main()
