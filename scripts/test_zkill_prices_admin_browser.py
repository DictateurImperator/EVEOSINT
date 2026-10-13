"""Browser verification with offline responses; never starts real imports."""

from unittest.mock import patch
from urllib.parse import urlsplit
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright, expect
from scripts.test_zkill_prices import routes


def main():
    app = FastAPI()
    app.include_router(routes.router)

    @app.get("/admin/jobs")
    def jobs_page(request: Request):
        context = routes.app_context(
            request=request,
            user=user,
            title="Jobs",
            active_module="admin",
            active_menu_key="admin.jobs",
        )
        context["jobs"] = [
            {
                "key": "import_zkill_prices",
                "type": "zkill_prices",
                "label": "Historical zKill prices",
                "enabled": True,
                "running": False,
                "log_exists": False,
            }
        ]
        return routes.templates.TemplateResponse(
            request=request, name="admin_jobs.html", context=context
        )

    client = TestClient(app)
    user = {
        "id": 1,
        "username": "admin",
        "permissions": {"admin.jobs.view", "admin.jobs.run"},
    }
    state = {"running": False, "progress": None}
    summary = {
        "days": 2,
        "items": 10794,
        "empty": 0,
        "first": "2003-01-01",
        "last": "2026-10-11",
        "bytes": 163840,
    }
    with patch.object(routes, "require_login", return_value=user), patch.object(
        routes, "summary", return_value=summary
    ), patch.object(
        routes, "zkill_prices_progress", side_effect=lambda: state
    ), patch.object(
        routes, "run_zkill_prices"
    ) as launch, patch.object(
        routes, "stop_zkill_prices"
    ) as stop, patch.object(
        routes, "audit_log"
    ), sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        def handle(route):
            parts = urlsplit(route.request.url)
            if parts.netloc != "prices.test":
                route.abort()
                return
            r = client.request(
                route.request.method,
                parts.path,
                content=route.request.post_data_buffer,
                headers={"content-type": route.request.headers.get("content-type", "")},
            )
            route.fulfill(status=r.status_code, body=r.content, headers=dict(r.headers))

        page.route("**/*", handle)
        page.goto("http://prices.test/admin/prices")
        expect(page.locator("#prices-summary")).to_contain_text("10,794")
        launch.assert_not_called()
        page.click("#prices-start")
        launch.assert_called_once_with("", "")
        state.update(
            running=True,
            progress={
                "phase": "running",
                "total_days": 8700,
                "processed": 3,
                "downloaded": 3,
                "current_day": "2003-01-03",
                "items": 300,
            },
        )
        expect(page.locator("#prices-start")).to_be_disabled(timeout=7000)
        expect(page.locator("#prices-status")).to_contain_text("2003-01-03")
        page.click("#prices-stop")
        stop.assert_called_once()
        state.update(running=False, progress={"phase": "paused"})
        expect(page.locator("#prices-start")).to_be_enabled(timeout=7000)
        # The actual Jobs template exposes the controls and live date on its card.
        launch.reset_mock()
        stop.reset_mock()
        page.goto("http://prices.test/admin/jobs")
        expect(page.locator("#prices-start")).to_be_visible()
        expect(page.locator("#prices-summary")).to_contain_text("10,794")
        page.fill('[name="date_from"]', "2003-01-01")
        page.fill('[name="date_to"]', "2003-01-03")
        page.click("#prices-start")
        launch.assert_called_once_with("2003-01-01", "2003-01-03")
        state.update(
            running=True,
            progress={
                "phase": "running",
                "from": "2003-01-01",
                "through": "2003-01-03",
                "total_days": 3,
                "processed": 1,
                "current_day": "2003-01-02",
            },
        )
        expect(page.locator("#prices-date")).to_have_text("2003-01-02", timeout=7000)
        expect(page.locator("#prices-range")).to_contain_text("2003-01-01 → 2003-01-03")
        expect(page.locator("[data-prices-job-header] .badge")).to_have_text("running")
        assert (
            abs(float(page.locator("#prices-bar").get_attribute("value")) - 100 / 3)
            < 0.01
        )
        page.click("#prices-stop")
        stop.assert_called_once()
        state.update(
            running=False,
            progress={
                "phase": "completed",
                "total_days": 3,
                "processed": 3,
                "last_success_day": "2003-01-03",
            },
        )
        expect(page.locator("#prices-date")).to_have_text("2003-01-03", timeout=7000)
        expect(page.locator("#prices-bar")).to_have_attribute("value", "100")
        expect(page.locator("[data-prices-job-header] .badge")).to_have_text(
            "completed"
        )
        user["permissions"].remove("admin.jobs.run")
        page.reload()
        expect(page.locator("#prices-form")).to_have_count(0)
        expect(page.locator("#prices-date")).to_have_text("2003-01-03")
        assert not errors, errors
        browser.close()
    print("Price import browser checks passed")


if __name__ == "__main__":
    main()
