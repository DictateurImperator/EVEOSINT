#!/usr/bin/env python3
"""Browser workflow using the disposable /tmp PostgreSQL lab and fake CCP.

Requires Playwright/Chromium and EVEOSINT_FORENSICS_TEST_SOCKET, as documented
in test_forensics_reconstruction.py. Never imports production configuration.
Run from the repository root: python -m scripts.test_forensics_browser
"""

import ast
import logging
import sys
import types
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from scripts import test_forensics_reconstruction as r


def main():
    if not r.SOCKET.startswith("/tmp/"):
        raise SystemExit(
            "Set EVEOSINT_FORENSICS_TEST_SOCKET to the disposable lab socket under /tmp."
        )
    lab = r.PersistenceTests()
    lab.setUp()
    try:
        run(lab)
    finally:
        lab.doCleanups()


def run(lab):
    f = r.f
    with lab.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE mer.killmails ADD COLUMN ccp_isk_lost numeric DEFAULT 123456789"
            )
            cur.execute(
                "UPDATE web.forensics_cases SET snapshot=snapshot||'{\"ccp_isk_lost\":123456789}'::jsonb"
            )
    app = FastAPI()
    # Exercise the actual global error normalizer, without importing live config.
    source = ast.parse(
        (Path(__file__).resolve().parents[1] / "web/app/main.py").read_text()
    )
    functions = []
    for node in source.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
            "_is_document_navigation",
            "_friendly_error_response",
            "normalize_error_responses",
        }:
            node.decorator_list = []
            functions.append(node)
    scope = {
        "Request": Request,
        "JSONResponse": JSONResponse,
        "PlainTextResponse": PlainTextResponse,
        "logger": logging.getLogger(__name__),
        "templates": f.routes.templates,
    }
    exec(
        compile(
            ast.Module(body=functions, type_ignores=[]),
            "production_error_handlers",
            "exec",
        ),
        scope,
    )
    app.middleware("http")(scope["normalize_error_responses"])
    app.include_router(f.routes.router)
    client = TestClient(app)
    user = {
        "id": 7,
        "username": "dev",
        "permissions": {f.routes.PERMISSION, "admin.jobs.run"},
    }
    row = list(f.mer_row(2))
    row[1] = r.AT.date().replace(day=1)
    row[3] = r.AT
    hidden = f.page_fixture(rows=[tuple(row)])
    errors = []
    correct = r.engine.killmail_hash(10, 21, 587, r.AT)
    state = {"http_calls": 0, "rate_limit": False, "status_failure": False}

    def fetch(kill_id, hash_value):
        state["http_calls"] += 1
        if state["rate_limit"]:
            state["rate_limit"] = False
            return 429, {"Retry-After": "3"}, {}
        return (
            (200, {}, r.payload())
            if hash_value == correct
            else (404, {}, {"error": "Invalid killmail hash"})
        )

    launches = []
    fake_jobs = types.ModuleType(f.PACKAGE + ".jobs")
    fake_jobs.JobError = RuntimeError
    fake_jobs.run_forensics_analysis_job = lambda *args, **kwargs: (
        launches.append((args, kwargs)) or (True, "started")
    )
    fake_audit = types.ModuleType(f.PACKAGE + ".audit")
    fake_audit.audit_log = lambda *args, **kwargs: None
    with (
        patch.dict(
            sys.modules,
            {f.PACKAGE + ".jobs": fake_jobs, f.PACKAGE + ".audit": fake_audit},
        ),
        patch.object(f.routes, "require_login", return_value=user),
        patch.object(f.routes, "get_hidden_killmails_page", return_value=hidden),
        patch.object(r.store, "fetch_ccp", side_effect=fetch),
        patch.object(
            f.routes.recovered_service,
            "post_zkill",
            return_value=(200, {}, {"status": "success"}),
        ) as zkill_post,
        sync_playwright() as p,
    ):
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1600, "height": 1150})
        page.on("pageerror", lambda error: errors.append(str(error)))

        def handle(route):
            req = route.request
            parts = urlsplit(req.url)
            if parts.netloc != "forensics.test":
                route.abort()
                return
            path = parts.path + ("?" + parts.query if parts.query else "")
            if parts.path.endswith("/analysis") and state["status_failure"]:
                route.fulfill(
                    status=502, content_type="text/html", body="<h1>Bad gateway</h1>"
                )
                return
            headers = {
                "Host": "forensics.test",
                "Accept": req.headers.get("accept", "*/*"),
            }
            if req.method == "POST":
                headers.update(
                    {
                        "Content-Type": "application/json",
                        "Origin": "http://forensics.test",
                    }
                )
            response = client.request(
                req.method, path, content=req.post_data, headers=headers
            )
            route.fulfill(
                status=response.status_code,
                headers={
                    "content-type": response.headers.get("content-type", "text/html")
                },
                body=response.content,
            )

        page.route("**/*", handle)
        # Missing setup must survive the global normalizer as actionable JSON.
        with patch.object(r.store, "ready", return_value=False):
            plain = client.get("/admin/killmail-forensics/analysis")
            assert plain.status_code == 503 and plain.text == "Erreur 503"
            response = client.get(
                "/admin/killmail-forensics/analysis",
                headers={"Accept": "application/json"},
            )
            page.goto("http://forensics.test/admin/killmail-forensics")
            expect(page.locator("[data-analysis-error]")).to_contain_text("Admin Jobs")
        assert response.status_code == 503
        assert response.json()["setup_required"]
        assert "Admin Jobs" in response.json()["error"]
        page.goto("http://forensics.test/admin/killmail-forensics")
        expect(page.locator("[data-analysis-error]")).to_be_hidden()
        state["status_failure"] = True
        page.reload()
        expect(page.locator("[data-analysis-error]")).to_contain_text("HTTP 502")
        assert "JSON.parse" not in page.locator("[data-analysis-error]").inner_text()
        state["status_failure"] = False
        expect(page.locator("[data-analysis-error]")).to_be_hidden(timeout=15000)
        expect(page.locator("[data-case]")).to_have_count(1)
        expect(page.locator("[data-credit-budget]")).to_contain_text("3,300 / 3,300")
        expect(page.locator("[data-case]")).to_contain_text("123,456,789 ISK")
        expect(page.locator("[data-forecast]")).to_contain_text("trials")
        assert "conditional" in page.locator("[data-forecast]").get_attribute("title")
        expect(page.locator("[data-case] img.killmail-type-icon")).to_have_count(2)
        expect(page.locator('[data-case] a[href="/corporation/100"]')).to_have_count(1)
        expect(page.locator('[data-case] a[href="/system/30000142"]')).to_have_count(1)
        assert "forensics-difficulty-" in page.locator("[data-case]").get_attribute(
            "class"
        )
        # All pilot flags must concern the same candidate in the chosen role.
        evidence = page.locator('[data-evidence-filter][value="same_ship_local"]')
        evidence.locator("xpath=ancestor::details").locator("summary").click()
        evidence.check()
        page.locator("[data-evidence-role]").select_option("victim")
        page.locator('[data-evidence-filter][value="final_blow_local"]').check()
        expect(page.locator("[data-case]")).to_have_count(0)
        page.locator("[data-evidence-role]").select_option("attacker")
        expect(page.locator("[data-case]")).to_have_count(1)
        page.locator("[data-evidence-clear]").click()
        expect(page.locator("[data-case]")).to_have_count(1)
        page.locator("[data-analysis-from]").fill("2026-08-01")
        page.locator("[data-analysis-to]").fill("2026-08-02")
        with page.expect_response("**/analysis/start"):
            page.locator("[data-analysis-start]").click()
        assert str(launches[-1][0][1]) == "2026-08-01"
        assert str(launches[-1][0][2]) == "2026-08-02"
        page.locator("[data-forensics-ref]").check()
        page.locator("[data-forensics-investigate]").click()
        expect(page.locator("[data-investigations-status]")).to_contain_text(
            "1 investigations ready"
        )
        case = page.locator("[data-case]")
        case.locator("details").nth(1).locator("summary").click()
        case.locator("details").nth(2).locator("summary").click()
        tooltip = (
            case.locator('[data-choice="victims"][value="10"]')
            .locator("..")
            .get_attribute("title")
        )
        assert "same ship and corporation" in tooltip
        # Draft choices change the count; applying them persists it across reload.
        case.locator('[data-choice="victims"][value="11"]').uncheck()
        expect(case.locator("[data-remaining]")).to_have_text("2 in draft")
        case.locator('[data-case-action="save"]').click()
        expect(case.locator("[data-remaining]")).to_have_text("2")
        page.reload()
        expect(page.locator("[data-remaining]")).to_have_text("2")
        case = page.locator("[data-case]")
        # Manual candidates stay visible after saving and can be removed again.
        case.locator('[data-manual="attackers"]').fill("25")
        with page.expect_response("**/cases?sort=date&page=1**"):
            page.locator("#forensics-sort").select_option("date")
        expect(page.locator("#forensics-investigations")).to_have_attribute(
            "aria-busy", "false"
        )
        expect(case.locator('[data-manual="attackers"]')).to_have_value("25")
        case.locator('[data-case-action="save"]').click()
        expect(case.locator("[data-remaining]")).to_have_text("3")
        case.locator("details").nth(2).locator("summary").click()
        case.locator('[data-choice="attackers"][value="25"]').uncheck()
        case.locator('[data-case-action="save"]').click()
        expect(case.locator("[data-remaining]")).to_have_text("2")
        page.locator("#forensics-sort").select_option("date")
        case.locator('[data-case-action="refresh"]').click()
        expect(page.locator("[data-investigations-status]")).to_contain_text(
            "Evidence refreshed"
        )
        expect(case.locator("[data-remaining]")).to_have_text("2")
        # A rate limit neither consumes a candidate nor survives Stop as a loop.
        state["rate_limit"] = True
        case.locator('[data-case-action="validate"]').click()
        expect(page.locator("[data-investigations-status]")).to_contain_text(
            "CCP returned 429"
        )
        expect(case.locator("[data-remaining]")).to_have_text("2")
        page.locator("[data-investigations-stop]").click()
        expect(page.locator("[data-investigations-status]")).to_contain_text(
            "Validation stopped"
        )
        page.reload()
        expect(page.locator("[data-remaining]")).to_have_text("2")
        lab.reset_gate()
        page.locator('[data-case-action="validate"]').click()
        expect(page.locator("[data-case]")).to_have_count(0, timeout=15000)
        expect(page.locator("[data-investigations-status]")).to_contain_text(
            "Killmail confirmed"
        )
        assert state["http_calls"] == 3
        page.get_by_role("link", name="Recovered killmails", exact=True).click()
        expect(page.locator("#recovered-table")).to_contain_text(correct)
        expect(page.locator("#recovered-table")).to_contain_text("123.46m ISK")
        expect(page.locator("#recovered-table")).to_contain_text(
            "2 hash tests to recover"
        )
        expect(page.locator("#recovered-table img.killmail-type-icon")).to_have_count(1)
        zkill_post.assert_not_called()
        # Shared recovered filters apply to confirmed attackers, and reset correctly.
        page.evaluate(
            "document.querySelector('[data-killboard-filters]').dispatchEvent(new CustomEvent('killboard:apply',{bubbles:true,detail:{params:new URLSearchParams({'builder_entity_include':'victim:character:999'})}}))"
        )
        expect(page.locator("[data-recovered-kill]")).to_have_count(0)
        page.evaluate(
            "document.querySelector('[data-killboard-filters]').dispatchEvent(new CustomEvent('killboard:apply',{bubbles:true,detail:{params:new URLSearchParams()}}))"
        )
        expect(page.locator("[data-recovered-kill]")).to_have_count(1)
        page.locator("#recovered-select-displayed").click()
        expect(page.locator("#recovered-selected")).to_have_text("1 selected")
        page.locator("#recovered-send-selected").click()
        expect(page.locator("[data-publication-state]")).to_have_text(
            "Accepted by zKillboard"
        )
        zkill_post.assert_called_once_with(1001, correct)
        expect(page.locator("[data-recovered-submit]")).to_be_disabled()
        page.reload()
        expect(page.locator("[data-publication-state]")).to_have_text(
            "Accepted by zKillboard"
        )
        zkill_post.assert_called_once()

        page.get_by_role("link", name="1001", exact=True).click()
        expect(
            page.get_by_role("heading", name="Recovered killmail 1001")
        ).to_be_visible()
        expect(page.locator("pre")).to_contain_text('"character_id": 10')
        expect(page.get_by_role("link", name="CCP killmail")).to_have_attribute(
            "href", f"https://esi.evetech.net/killmails/1001/{correct}/"
        )
        assert not errors, errors
        browser.close()
    print(
        "Browser checks passed: investigation selection, evidence tooltips, choices, manual candidates, reload, sorting, refresh, rate limit, stop/resume, recovery and full killmail consultation."
    )


if __name__ == "__main__":
    main()
