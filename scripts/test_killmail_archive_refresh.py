"""Offline archive refresh, Admin access and disposable PostgreSQL integration."""

import io
import json
import os
import tarfile
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import psycopg2
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts import refresh_killmail_archives as refresh
from scripts import test_killmail_forensics as f

stats = f.load("killmail_statistics")
mer = f.load("mer")
stat_routes = f.load("routes_admin_killmail_statistics")
DAY = date(2026, 8, 29)
STAMP = "2026-08-29T18:04:34Z"


def payload(kmid=138072306, stamp=STAMP):
    return {"killmail_id": kmid, "killmail_time": stamp, "solar_system_id": 30000142,
            "victim": {"character_id": 2123439234, "corporation_id": 98473379,
                       "ship_type_id": 20187, "damage_taken": 100},
            "attackers": [{"character_id": 2124202187, "corporation_id": 98658732,
                           "ship_type_id": 12034, "final_blow": True, "damage_done": 100}]}


def archive_bytes(payloads):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:bz2") as archive:
        for item in payloads:
            data = json.dumps(item).encode()
            info = tarfile.TarInfo(f"killmails/{item['killmail_id']}.json")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return out.getvalue()


class Response:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def raise_for_status(self):
        pass

    def json(self):
        return self.data

    def iter_content(self, chunk_size):
        yield self.data


class Source:
    def __init__(self, data, count=2):
        self.data = data
        self.count = count
        self.calls = []
        self.entry = {"name": f"killmails-{DAY}.tar.bz2", "etag": "updated",
                      "last_modified": "2026-10-09T11:00:17Z", "size": len(data)}

    def get(self, url, **_kwargs):
        self.calls.append(url)
        if url.endswith("totals.json"):
            return Response({DAY.isoformat(): self.count})
        if url.endswith("index.json"):
            return Response({"files": [self.entry]})
        if url.endswith(".tar.bz2"):
            return Response(self.data)
        raise AssertionError("Unexpected URL: " + url)


class DecisionTests(unittest.TestCase):
    def test_initial_baseline_and_count_growth(self):
        entry = {"etag": "new", "last_modified": "now", "size": 100}
        self.assertFalse(refresh.needs_refresh(17705, 17705, None, entry))
        self.assertTrue(refresh.needs_refresh(17705, 18114, None, entry))
        self.assertTrue(refresh.needs_refresh(None, None, None, entry))
        with self.assertRaises(ValueError):
            refresh.needs_refresh(18114, 17705, None, entry)

    def test_index_changes_without_count_growth(self):
        entry = {"etag": "new", "last_modified": "now", "size": 100}
        self.assertFalse(refresh.needs_refresh(2, 2, dict(entry), entry))
        self.assertTrue(refresh.needs_refresh(2, 2, {"etag": "old"}, entry))

    def test_index_only_accepts_daily_archives_in_requested_year(self):
        source = Source(b"unused")
        source.entry["url"] = "https://untrusted.invalid/archive"
        entries = refresh.year_index(source, 2026)
        self.assertEqual(list(entries), [DAY.isoformat()])
        self.assertEqual(refresh.year_index(source, 2025), {})

    def test_job_is_passive_and_validates_dates_and_import_conflicts(self):
        import sys
        config = sys.modules[f.PACKAGE + ".config"]
        with patch.object(config, "JOBS_CONFIG_PATH", f.ROOT / "config/offline-jobs.json", create=True):
            jobs = f.load("jobs")
        with patch.object(jobs, "_read_config_file", return_value={"jobs": []}), \
             patch.object(jobs, "_start_process", return_value=(True, "started")) as launch, \
             patch.object(jobs, "_cleanup_stale_pid", return_value=(False, None)) as running:
            job = jobs.get_job("refresh_killmail_archives")
            self.assertEqual(job["type"], "killmail_archive_refresh")
            launch.assert_not_called()
            jobs.run_killmail_archive_refresh_job("2026-08-01", "2026-08-31")
            self.assertEqual(launch.call_args.args[1][-4:], ["--from", "2026-08-01", "--to", "2026-08-31"])
            launch.reset_mock()
            with self.assertRaises(jobs.JobError):
                jobs.run_killmail_archive_refresh_job("2026-09-01", "2026-08-01")
            launch.assert_not_called()
            running.return_value = (True, 123)
            with self.assertRaises(jobs.JobError):
                jobs.run_killmail_archive_refresh_job()
            with self.assertRaises(jobs.JobError):
                jobs.run_killmail_job("2026-08-01", "2026-08-31", 4)
            launch.assert_not_called()

    def test_statistics_access_and_year_validation(self):
        app = FastAPI()
        app.include_router(stat_routes.router)
        client = TestClient(app)
        user = {"id": 1, "username": "admin", "permissions": {"admin.jobs.view"}}
        with patch.object(stat_routes, "require_login", return_value=user), \
             patch.object(stat_routes, "monthly_statistics", return_value={"year": 2026}) as query:
            self.assertEqual(client.get("/admin/killmail-statistics/data?year=2026").json(), {"year": 2026})
            query.assert_called_once_with(2026)
            self.assertEqual(client.get("/admin/killmail-statistics/data?year=bad").status_code, 422)
            self.assertEqual(client.get("/admin/killmail-statistics").status_code, 200)
            user["permissions"] = set()
            self.assertEqual(client.get("/admin/killmail-statistics/data").status_code, 403)
            query.reset_mock()
            self.assertEqual(client.get("/admin/killmail-statistics", follow_redirects=False).status_code, 302)
            query.assert_not_called()
        with patch.object(stat_routes, "require_login", return_value=None):
            self.assertEqual(client.get("/admin/killmail-statistics/data").status_code, 401)


SOCKET = os.environ.get("EVEOSINT_FORENSICS_TEST_SOCKET", "")


@unittest.skipUnless(SOCKET.startswith("/tmp/"), "Requires disposable lab socket under /tmp on port 55444")
class RefreshIntegrationTests(unittest.TestCase):
    def connect(self):
        return psycopg2.connect(host=SOCKET, port=55444, dbname="postgres", user="codex")

    def setUp(self):
        from scripts import sync_killmails as importer
        self.importer = importer
        self.temporary = tempfile.TemporaryDirectory(prefix="archive-refresh-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.state = self.base / "refresh_index.json"
        self.patch = patch.object(importer, "ARCHIVE_DIR", self.base / "archives")
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.old = payload(138072305, "2026-08-29T18:04:33Z")
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DROP SCHEMA IF EXISTS rawkm CASCADE; DROP SCHEMA IF EXISTS mer CASCADE; DROP SCHEMA IF EXISTS web CASCADE; CREATE SCHEMA web")
            importer.ensure_tables(conn)
            importer.ensure_month_partitions(conn, DAY, DAY)
            refresh.ensure_recovery_table(conn)
            importer.insert_killmail(conn, self.old)
            importer.mark_day(conn, DAY, "success", files_count=1, archive_count=1)
            mer._ensure_mer_kill_tables(conn)
            for month in (date(2026, 8, 1), date(2026, 9, 1)):
                mer._ensure_mer_kill_partition(conn, month)
            with conn.cursor() as cur:
                cur.execute((f.ROOT / "web/app/forensics_schema.sql").read_text())
                for row, at in ((1, STAMP), (2, "2026-09-01T00:00:00Z")):
                    cur.execute("""INSERT INTO mer.killmails(source_month,source_row,kill_datetime,
                        solar_system_id,victim_ship_type_id,victim_corporation_id,killer_corporation_id)
                        VALUES (%s,%s,%s,30000142,20187,98473379,98658732)""",
                        (at[:7] + "-01", row, at))
        self.importer.archive_tar_path(DAY).write_bytes(archive_bytes([self.old]))
        self.source = Source(archive_bytes([self.old, payload()]))

    def run_job(self, source=None, match=None):
        with self.connect() as conn, patch.object(mer, "db", self.connect), patch.object(mer, "_mer_log"):
            return refresh.run_refresh(conn, self.importer, source or self.source, self.state,
                                       match or mer.match_mer_killmails)

    def scalar(self, sql):
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()[0]

    def statistics(self):
        with patch.object(stats, "db", self.connect):
            return stats.monthly_statistics(2026)

    def test_late_kill_is_imported_matched_counted_once_and_not_redownloaded(self):
        result = self.run_job()
        self.assertEqual(result, {"checked": 1, "changed": 1, "added": 1, "failed": 0})
        self.assertEqual(self.scalar("SELECT resolved_km[1] FROM mer.killmails WHERE source_row=1"), 138072306)
        self.assertEqual(self.scalar("SELECT count(*) FROM rawkm.killmail_archive_recoveries"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM rawkm.killmail_attackers"), 2)
        self.assertEqual(self.scalar("SELECT files_count FROM rawkm.killmail_import_days"), 2)
        page = self.statistics()
        self.assertEqual(page["totals"]["recovered_archives"], 1)
        self.assertEqual(page["totals"]["hidden"], 1)
        before = len(self.source.calls)
        self.assertEqual(self.run_job()["changed"], 0)
        self.assertFalse(any(url.endswith(".tar.bz2") for url in self.source.calls[before:]))
        self.assertEqual(self.scalar("SELECT count(*) FROM rawkm.killmail_attackers"), 2)
        self.assertEqual(json.loads(self.state.read_text())["pending_months"], [])

    def test_stale_archive_and_failed_match_are_resumed(self):
        with self.assertRaises(RuntimeError):
            self.run_job(match=Mock(return_value={"busy": True}))
        self.assertEqual(self.scalar("SELECT count(*) FROM rawkm.killmails"), 2)
        self.assertEqual(json.loads(self.state.read_text())["pending_months"], ["2026-08"])
        self.source.calls.clear()
        result = self.run_job()
        self.assertEqual(result["added"], 0)
        self.assertFalse(any(url.endswith(".tar.bz2") for url in self.source.calls))
        self.assertEqual(self.scalar("SELECT resolved_km[1] FROM mer.killmails WHERE source_row=1"), 138072306)

    def test_corrupt_download_preserves_old_cache_and_can_retry(self):
        original = self.importer.archive_tar_path(DAY).read_bytes()
        bad = Source(b"not a tar archive")
        with self.assertLogs(refresh.LOG, level="ERROR"):
            with self.assertRaises(RuntimeError):
                self.run_job(bad)
        self.assertEqual(self.importer.archive_tar_path(DAY).read_bytes(), original)
        self.assertEqual(self.scalar("SELECT files_count FROM rawkm.killmail_import_days"), 1)
        self.assertEqual(list(self.importer.archive_tar_path(DAY).parent.glob(".refresh-*")), [])
        self.assertEqual(self.run_job()["added"], 1)

    def test_month_scope_preserves_other_month_and_default_match_still_works(self):
        september = payload(138072307, "2026-09-01T00:00:00Z")
        with self.connect() as conn:
            self.importer.ensure_month_partitions(conn, date(2026, 9, 1), date(2026, 9, 1))
            self.importer.insert_killmail(conn, september)
        self.run_job()
        self.assertIsNone(self.scalar("SELECT resolved_km FROM mer.killmails WHERE source_row=2"))
        with patch.object(mer, "db", self.connect), patch.object(mer, "_mer_log"):
            mer.match_mer_killmails()
        self.assertEqual(self.scalar("SELECT resolved_km[1] FROM mer.killmails WHERE source_row=2"), 138072307)

    def test_confirmed_forensics_wins_without_double_counting_archive_recovery(self):
        self.run_job()
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO web.forensics_cases(kill_datetime,source_month,source_row,
                snapshot,hypotheses,choices,status,recovered_killmail_id)
                VALUES (%s,'2026-08-01',1,'{}','{}','{}','recovered',138072306) RETURNING id""", (STAMP,))
            case_id = cur.fetchone()[0]
            cur.execute("INSERT INTO web.forensics_recovered(killmail_id,hash,case_id,payload) VALUES(138072306,%s,%s,%s)",
                        ("a" * 40, case_id, json.dumps(payload())))
        totals = self.statistics()["totals"]
        self.assertEqual(totals["recovered"], 1)
        self.assertEqual(totals["recovered_forensics"], 1)
        self.assertEqual(totals["recovered_archives"], 0)
        self.assertEqual(totals["mer_total"], sum(totals[key] for key in ("known", "hidden", "ambiguous", "recovered")))

    def test_parallel_mer_match_is_refused_until_the_other_match_finishes(self):
        with self.connect() as lock_conn, lock_conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(472119050)")
            try:
                with patch.object(mer, "db", self.connect), patch.object(mer, "_mer_log"):
                    self.assertEqual(mer.match_mer_killmails(), {"busy": True})
            finally:
                cur.execute("SELECT pg_advisory_unlock(472119050)")
        self.assertEqual(self.run_job()["added"], 1)

    def test_stats_before_any_optional_recovery_tables_exist(self):
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("DROP TABLE rawkm.killmail_archive_recoveries; DROP SCHEMA web CASCADE")
        page = self.statistics()
        self.assertFalse(page["archive_recovery_tracking"])
        self.assertEqual(page["totals"]["hidden"], 2)
        self.assertEqual(page["totals"]["archive_killmails"], 1)


if __name__ == "__main__":
    unittest.main()
