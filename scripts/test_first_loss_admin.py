"""Offline permissions, manual job controls and passive progress checks."""

import json, os, sys, unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from scripts import test_killmail_forensics as f

config = sys.modules[f.PACKAGE + ".config"]
with patch.object(
    config, "JOBS_CONFIG_PATH", f.ROOT / "config/offline-jobs.json", create=True
):
    jobs = f.load("jobs")
routes = f.load("routes_admin_first_loss")


class AdminTests(unittest.TestCase):
    def test_job_is_manual_and_validates_parameters(self):
        with patch.object(
            jobs, "_read_config_file", return_value={"jobs": []}
        ), patch.object(jobs, "_start_process") as launch:
            job = jobs.get_job("analyze_first_losses")
            self.assertEqual(job["type"], "first_loss_study")
            launch.assert_not_called()
            jobs.run_first_loss_study()
            launch.assert_called_once_with(job, job["command"] + ["--clusters", "auto"])
            launch.reset_mock()
            jobs.run_first_loss_study(5, True)
            launch.assert_called_once_with(
                job, job["command"] + ["--clusters", "5", "--rebuild"]
            )
            launch.reset_mock()
            for value in (999, "invalid", "auto; echo unsafe", "1", "2.5"):
                with self.assertRaises(jobs.JobError):
                    jobs.run_first_loss_study(value)
            launch.assert_not_called()

    def test_only_the_registered_process_can_be_stopped(self):
        job = {
            "key": "analyze_first_losses",
            "command": ["python", "/app/scripts/analyze_first_losses.py"],
        }
        with patch.object(jobs, "get_job", return_value=job), patch.object(
            jobs, "_cleanup_stale_pid", return_value=(True, 123)
        ), patch.object(
            Path, "read_bytes", return_value=b"python\0/unrelated.py\0"
        ), patch.object(
            os, "kill"
        ) as kill:
            with self.assertRaises(jobs.JobError):
                jobs.stop_first_loss_study()
            kill.assert_not_called()

    def test_stale_progress_cannot_appear_as_the_new_run(self):
        with patch.object(
            jobs, "_read_runtime_status", return_value={"running": True, "pid": 2}
        ), patch.object(
            Path, "read_text", return_value=json.dumps({"pid": 1, "phase": "completed"})
        ):
            self.assertEqual(
                jobs.first_loss_progress()["progress"]["phase"], "starting"
            )
        with patch.object(
            jobs, "_read_runtime_status", return_value={"running": False, "pid": None}
        ), patch.object(
            Path, "read_text", return_value=json.dumps({"pid": 1, "phase": "followup"})
        ):
            self.assertEqual(
                jobs.first_loss_progress()["progress"]["phase"], "interrupted"
            )

    def test_view_and_run_permissions_are_separate(self):
        app = FastAPI()
        app.include_router(routes.router)
        client = TestClient(app)
        user = {"id": 1, "username": "admin", "permissions": {"admin.jobs.view"}}
        with patch.object(routes, "require_login", return_value=user), patch.object(
            routes, "run_first_loss_study"
        ) as launch, patch.object(
            routes, "stop_first_loss_study"
        ) as stop, patch.object(
            routes,
            "first_loss_progress",
            return_value={"running": False, "progress": None},
        ), patch.object(
            routes, "report", return_value=None
        ), patch.object(
            routes, "audit_log"
        ), patch.object(
            routes, "read_job_log", return_value="DONE"
        ) as log:
            response = client.get("/admin/debug/first-loss/status")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(client.get("/admin/debug/first-loss/log").text, "DONE")
            self.assertEqual(
                client.post("/admin/debug/first-loss/start").status_code, 403
            )
            self.assertEqual(
                client.post("/admin/debug/first-loss/stop").status_code, 403
            )
            launch.assert_not_called()
            stop.assert_not_called()
            user["permissions"].add("admin.jobs.run")
            self.assertEqual(
                client.post(
                    "/admin/debug/first-loss/start",
                    data={"clusters": 5, "rebuild": "true"},
                ).json(),
                {"started": True},
            )
            launch.assert_called_once_with("5", True)
            launch.reset_mock()
            self.assertEqual(
                client.post("/admin/debug/first-loss/start").json(),
                {"started": True},
            )
            launch.assert_called_once_with("auto", False)
            launch.side_effect = jobs.JobError("already running")
            self.assertEqual(
                client.post("/admin/debug/first-loss/start").status_code, 409
            )
            self.assertEqual(
                client.post("/admin/debug/first-loss/stop").json(),
                {"stop_requested": True},
            )
            user["permissions"] = set()
            log.reset_mock()
            for endpoint in ("", "/status", "/log"):
                self.assertEqual(
                    client.get("/admin/debug/first-loss" + endpoint).status_code, 403
                )
            log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
