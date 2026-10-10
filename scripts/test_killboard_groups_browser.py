"""Offline Chromium checks for shared group filters and global All/Hidden modes."""

from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader
from playwright.sync_api import expect, sync_playwright

from scripts import test_killmail_forensics as f
from scripts.test_killboard_modes import OPTIONS


def main():
    environment = Environment(loader=ChoiceLoader([
        DictLoader({"base.html": '<!doctype html><html><head><meta charset="utf-8"><style>.d-none{display:none}</style></head><body>{% block content %}{% endblock %}</body></html>'}),
        FileSystemLoader(f.ROOT / "web/templates"),
    ]))
    result = f.page_fixture(limit=2)
    fragment = environment.get_template("entity_killmails_fragment.html").render(killmail_page=result, killmail_mode="hidden")
    empty = '<table class="killmail-table"><tbody></tbody></table>'
    errors, scans = [], []
    with patch.object(f.filters, "get_killmail_ship_options", return_value=OPTIONS), sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))

        def handle(route):
            url = urlsplit(route.request.url)
            query = parse_qs(url.query)
            if url.netloc != "killboard.test":
                route.abort()
            elif url.path == "/killboard/search":
                route.fulfill(json={"results": f.filters.search_killboard_filters(query["kind"][0], query["q"][0])})
            elif url.path == "/killboard/scan":
                scans.append(query)
                # Force an empty window with only a timestamp cursor. The next
                # request must still run, even without X-KB-Next-Month.
                continuing = "scan_before" in query
                headers = {"X-KB-Matches": "2" if continuing else "0", "X-KB-Scanned": "0",
                           "X-KB-Scan-Complete": "1" if continuing else "0"}
                if not continuing:
                    headers["X-KB-Next-Before"] = "2026-10-08T12:00:00+00:00"
                route.fulfill(content_type="text/html", body=fragment if continuing else empty, headers=headers)
            elif url.path == "/killboard/data":
                route.fulfill(content_type="text/html", body=fragment)
            elif url.path.startswith("/derived/"):
                entity = url.path.rsplit("/", 1)[-1]
                family = "ship" if entity == "ship" else "entity"
                component = environment.get_template("killboard_filters.html").render(
                    killboard_context_family=family, killboard_context_type=entity, killboard_context_id=42)
                body = '<!doctype html><html><head><meta charset="utf-8"></head><body>' + component + '''
                    <script>document.querySelector('[data-killboard-filters]').addEventListener('killboard:apply',
                    event => { window.appliedFilters = event.detail.params.toString(); });</script></body></html>'''
                route.fulfill(content_type="text/html", body=body)
            else:
                mode = query.get("mode", ["api"])[0]
                body = environment.get_template("killboard.html").render(
                    killmail_mode=mode, killmail_page=None, killboard_defer_scan=mode != "api")
                route.fulfill(content_type="text/html", body=body)

        page.route("**/*", handle)
        page.goto("http://killboard.test/killboard")
        page.locator("[data-kb-toggle]").click()
        page.locator('[data-kb-search="ship"]').fill("frigate")
        choice = page.locator('[data-kb-results="ship"]').get_by_text("Frigate · all types", exact=True)
        expect(choice).to_be_visible()
        choice.click()
        page.locator('[data-kb-role="ship:0"]').select_option("victim")
        page.locator('[data-kb-apply]').click()
        expect(page.locator("[data-kb-progress-title]")).to_have_text("Search complete.")
        assert scans[-1]["builder_ship_include"] == ["victim:group:Frigate"], scans
        assert "scan_before" in scans[-1], scans
        for mode, label in [("total", "All"), ("hidden", "Hidden")]:
            before = len(scans)
            page.get_by_role("button", name=label, exact=True).click()
            expect(page.locator("[data-kb-progress-title]")).to_have_text("Search complete.")
            assert len(scans) == before + 2, scans
            assert scans[-1]["mode"] == [mode], scans
            assert scans[-1]["builder_ship_include"] == ["victim:group:Frigate"], scans
        page.locator('[data-kb-sign="ship:0"]').click()
        page.locator('[data-kb-apply]').click()
        expect(page.locator("[data-kb-progress-title]")).to_have_text("Search complete.")
        assert scans[-1]["mode"] == ["hidden"]
        assert scans[-1]["builder_ship_exclude"] == ["victim:group:Frigate"]
        page.reload()
        page.locator("[data-kb-toggle]").click()
        expect(page.locator('[data-kb-terms="ship"]')).to_contain_text("Frigate · all types")
        expect(page.locator('[data-kb-role="ship:0"]')).to_have_value("victim")
        expect(page.locator('[data-kb-sign="ship:0"]')).to_have_text("−")
        # The same builder used by derived killboards preserves group IDs,
        # role and exclusion when hydrated from a bookmarked URL.
        for entity in ("ship", "corporation", "alliance"):
            page.goto("http://killboard.test/derived/" + entity + "?builder_ship_exclude=attacker:group:Frigate")
            page.locator("[data-kb-toggle]").click()
            expect(page.locator('[data-kb-terms="ship"]')).to_contain_text("Frigate · all types")
            page.locator('[data-kb-apply]').click()
            applied = parse_qs(page.evaluate("window.appliedFilters"))
            assert applied["builder_ship_exclude"] == ["attacker:group:Frigate"], applied
        assert not errors, errors
        browser.close()
    print("Shared ship group and All/Hidden killboard browser checks passed.")


if __name__ == "__main__":
    main()
