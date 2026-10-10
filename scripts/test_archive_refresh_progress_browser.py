"""Offline Chromium checks for live progress in Admin Jobs; no real worker."""

import sys
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from scripts import test_killmail_forensics as f


def main():
    config = sys.modules[f.PACKAGE + ".config"]
    with patch.object(config, "JOBS_CONFIG_PATH", f.ROOT / "config/offline-jobs.json", create=True):
        f.load("jobs")
        routes = f.load("routes_admin_jobs")
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)
    user = {"id": 7, "username": "admin", "permissions": {"admin.jobs.view", "admin.jobs.run"}}
    job = {"key": "refresh_killmail_archives", "label": "Killmails · Refresh updated EVE Ref archives",
           "type": "killmail_archive_refresh", "enabled": True, "running": False,
           "last_lines": "", "log_exists": False}
    snapshot = {"running": True, "progress": {"phase": "checking", "scanned": 100, "archives_total": 1000,
                "to_update": 4, "scan_complete": False, "current_year": 2026}}
    errors, bad_response = [], False
    with patch.object(routes, "require_login", return_value=user), \
         patch.object(routes, "list_jobs_with_logs", return_value=[job]), \
         patch.object(routes, "read_killmail_archive_refresh_progress", side_effect=lambda: snapshot), \
         patch.object(routes, "read_killmail_archive_errors", return_value="ValueError: sample earlier error"), \
         patch.object(routes, "run_killmail_archive_refresh_job") as launch, sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))

        def handle(route):
            parts = urlsplit(route.request.url)
            if parts.netloc != "jobs.test":
                route.abort()
                return
            if parts.path.endswith("/progress") and bad_response:
                route.fulfill(status=502, content_type="text/html", body="Bad gateway")
                return
            response = client.get(parts.path)
            route.fulfill(status=response.status_code, content_type=response.headers.get("content-type", "text/html"), body=response.content)

        page.context.route("**/*", handle)
        page.goto("http://jobs.test/admin/jobs")
        get = lambda name: page.locator("[data-refresh-" + name + "]")
        expect(get("total")).to_have_text("4 found (checking)")
        expect(get("checked")).to_have_text("100 / 1,000")
        expect(page.get_by_role("button", name="Stop archive refresh")).to_be_visible()
        expect(page.get_by_role("button", name="Refresh updated archives")).to_be_disabled()
        snapshot["progress"].update(phase="downloading", scan_complete=True, scanned=1000,
                                   to_update=7, processed=0, current_day="2026-08-29",
                                   bytes_downloaded=1048576, bytes_total=3145728)
        expect(get("total")).to_have_text("7")
        expect(get("current")).to_contain_text("2026-08-29 · 1.0 MB / 3.0 MB")
        snapshot["progress"].update(phase="importing", processed=2, updated=2, added=42, files_read=500)
        expect(get("processed")).to_have_text("2 / 7")
        expect(get("added")).to_have_text("42")
        expect(get("current")).to_contain_text("500 killmails read")
        snapshot["running"] = False
        snapshot["progress"].update(phase="completed", processed=7, updated=7, added=150, current_day=None)
        expect(get("state")).to_have_text("Completed")
        expect(get("processed")).to_have_text("7 / 7")
        expect(page.get_by_role("button", name="Stop archive refresh")).to_be_hidden()
        expect(page.get_by_role("button", name="Refresh updated archives")).to_be_enabled()
        bad_response = True
        expect(get("error")).to_be_visible(timeout=6000)
        expect(get("error")).to_contain_text("Unable to read progress")
        bad_response = False
        expect(get("error")).to_be_hidden(timeout=6000)
        with page.expect_popup() as opened:
            page.get_by_role("link", name="View errors (including earlier entries)").click()
        error_page = opened.value
        expect(error_page.locator("body")).to_contain_text("ValueError: sample earlier error")
        expect(error_page.locator("body")).to_contain_text("Last 20 error blocks")
        error_page.close()
        assert not errors, errors
        launch.assert_not_called()
        browser.close()
    print("Admin archive progress browser checks passed.")


if __name__ == "__main__":
    main()
