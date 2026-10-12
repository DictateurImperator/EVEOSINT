"""Read-only export permissions, legacy snapshots and searchable PDF contents."""

import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from scripts.test_first_loss_admin import routes
from scripts.test_first_loss_admin_browser import fixture


class PDFTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)
        self.user = dict(id=1, username="viewer", permissions={"admin.jobs.view"})

    def test_export_is_view_only_and_never_launches_a_job(self):
        with patch.object(
            routes, "require_login", return_value=self.user
        ), patch.object(routes, "report", return_value=fixture()), patch.object(
            routes, "run_first_loss_study"
        ) as launch:
            response = self.client.get(
                "/admin/debug/first-loss/report.pdf?horizon=90&year=2012&scope=pvp"
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "application/pdf")
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertIn("attachment;", response.headers["content-disposition"])
            self.assertTrue(response.content.startswith(b"%PDF-1.4"))
            self.assertTrue(response.content.endswith(b"%%EOF\n"))
            launch.assert_not_called()
            self.user["permissions"] = set()
            self.assertEqual(
                self.client.get("/admin/debug/first-loss/report.pdf").status_code, 403
            )
            self.assertEqual(routes.report.call_count, 1)

    def test_invalid_or_incomplete_scopes_are_explicit(self):
        with patch.object(
            routes, "require_login", return_value=self.user
        ), patch.object(routes, "report", return_value=None) as report:
            self.assertEqual(
                self.client.get("/admin/debug/first-loss/report.pdf").status_code, 404
            )
            report.return_value = dict(status="paused", summary=None)
            self.assertEqual(
                self.client.get("/admin/debug/first-loss/report.pdf").status_code, 409
            )
            report.return_value = fixture()
            self.assertEqual(
                self.client.get(
                    "/admin/debug/first-loss/report.pdf?year=2009"
                ).status_code,
                404,
            )
            for query in (
                "horizon=1",
                "year=bad",
                "scope=bad",
                "year=2012%0D%0AX-Test:bad",
            ):
                self.assertEqual(
                    self.client.get(
                        "/admin/debug/first-loss/report.pdf?" + query
                    ).status_code,
                    422,
                )
            report.side_effect = RuntimeError("offline database")
            with self.assertLogs(routes.LOG, level="ERROR"):
                self.assertEqual(
                    self.client.get("/admin/debug/first-loss/report.pdf").status_code,
                    503,
                )

    def test_all_axes_and_supervised_effects_are_searchable(self):
        snapshot = fixture()
        pca = snapshot["summary"]["pca"]["global"]
        pca["dimensions"] = 52
        pca["spectrum"] = [
            dict(
                component=i,
                variance_ratio=1 / 52,
                cumulative_ratio=i / 52,
                retained=i < 48,
                top_features=[dict(name="age_days", coefficient=0.5)],
            )
            for i in range(1, 53)
        ]
        snapshot["summary"]["return_model"] = dict(
            scopes={
                "pvp": {
                    "90": dict(
                        training_n=160,
                        test_n=40,
                        brier=0.12,
                        baseline_brier=0.24,
                        brier_skill=0.5,
                        auc=0.75,
                        effects=[
                            dict(
                                factor="age",
                                profile="1-5 years",
                                reference="<7 days",
                                effect_pp=-18.5,
                                interval_pp=[-22, -15],
                            )
                        ],
                        importance=[
                            dict(
                                factor="age",
                                importance_percent=100,
                                delta_brier=0.05,
                                interval=[0.04, 0.06],
                            )
                        ],
                    )
                }
            }
        )
        snapshot["summary"]["data_quality"] = dict(
            invalid_birthdays=10, missing_features={}
        )
        content = routes.make_report(snapshot, "90", "all", "pvp")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.pdf"
            path.write_bytes(content)
            info = subprocess.run(
                ["pdfinfo", str(path)], capture_output=True, text=True, check=True
            ).stdout
            text = subprocess.run(
                ["pdftotext", "-layout", str(path), "-"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        self.assertIn("PDF version:     1.4", info)
        for value in (
            "CLASSIFIED",
            "THE IMPERIUM",
            "PC52",
            "-18.50 pp",
            "Scientific methodology",
            "Adjusted difference",
            "100.00%",
        ):
            self.assertIn(value, text)
        self.assertNotIn("PRELIMINARY SNAPSHOT", text)

    def test_legacy_diagnostics_do_not_invent_model_results(self):
        snapshot = fixture()
        snapshot["summary"].pop("pca")
        snapshot["summary"].pop("factors_by_year")
        content = routes.make_report(snapshot)
        self.assertGreater(len(content), 1000)


if __name__ == "__main__":
    unittest.main()
