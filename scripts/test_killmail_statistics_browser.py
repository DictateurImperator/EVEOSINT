"""Offline Chromium checks for monthly statistics; no external API or database."""

from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from scripts.test_killmail_archive_refresh import stat_routes


def main():
    app = FastAPI()
    app.include_router(stat_routes.router)
    client = TestClient(app)
    user = {"id": 7, "username": "admin", "permissions": {"admin.jobs.view", "admin.killmail_forensics.dev"}}
    calls, errors = [], []
    bad_response = False

    def data(year):
        calls.append(year)
        selected = year or 2026
        totals = {"known": 100, "hidden": 20, "recovered": 5, "coverage": 84.0}
        rows = [{"month": f"{selected}-08", "archive_killmails": 1000, "mer_total": 125,
                 "known": 100, "hidden": 20, "recovered": 5, "ambiguous": 0,
                 "recovered_archives": 3, "recovered_forensics": 2, "coverage": 84.0}]
        return {"year": selected, "years": [2026, 2025], "totals": totals,
                "months": rows, "mer_available": True}

    with patch.object(stat_routes, "require_login", return_value=user), \
         patch.object(stat_routes, "monthly_statistics", side_effect=data), sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))

        def handle(route):
            parts = urlsplit(route.request.url)
            if parts.netloc != "statistics.test":
                route.abort()
                return
            if parts.path.endswith("/data") and bad_response:
                route.fulfill(status=502, content_type="text/html", body="Bad gateway")
                return
            response = client.get(parts.path + ("?" + parts.query if parts.query else ""))
            route.fulfill(status=response.status_code, content_type=response.headers.get("content-type", "text/html"), body=response.content)

        page.route("**/*", handle)
        page.goto("http://statistics.test/admin/killmail-statistics")
        expect(page.locator("#km-stat-rows")).to_contain_text("2026-08")
        expect(page.locator("#km-stat-rows")).to_contain_text("3 archives · 2 Forensics")
        expect(page.locator("#km-stat-summary")).to_contain_text("Recovered MER losses")
        expect(page.locator("#km-stat-refresh")).to_be_enabled()
        page.locator("#km-stat-year").select_option("2025")
        expect(page.locator("#km-stat-rows")).to_contain_text("2025-08")
        assert calls == [None, 2025], calls
        expect(page.locator(".km-stat-bar span")).to_have_count(4)
        bad_response = True
        page.locator("#km-stat-refresh").click()
        expect(page.locator("#km-stat-error")).to_be_visible()
        expect(page.locator("#km-stat-error")).to_contain_text("Unable to load statistics")
        expect(page.locator("#km-stat-refresh")).to_be_enabled()
        bad_response = False
        page.locator("#km-stat-refresh").click()
        expect(page.locator("#km-stat-error")).to_be_hidden()
        assert not errors, errors
        browser.close()
    print("Monthly statistics browser checks passed.")


if __name__ == "__main__":
    main()
