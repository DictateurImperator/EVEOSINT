"""Offline Chromium regression for wormhole heat and filtered killboard links.

Run with: python -m scripts.test_map_anoikis_browser
No production server, archives or database are accessed.
"""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    environment = Environment(loader=ChoiceLoader([
        DictLoader({"base.html": '<!doctype html><html><head><meta charset="utf-8"></head><body>{% block content %}{% endblock %}</body></html>'}),
        FileSystemLoader(ROOT / "web/templates"),
    ]))
    payload = {
        "breadcrumbs": [], "subtitle": "Offline map",
        "nodes": [
            {"id": 30000142, "name": "Jita", "x": 0, "y": 0, "security": .9},
            {"id": 30000144, "name": "Perimeter", "x": 100, "y": 100, "security": .9},
        ],
        "edges": [],
        "anoikis_nodes": [{"id": 31000001, "name": "J055520", "layout_x": 100,
                           "layout_y": 100, "security": -1, "space": "anoikis",
                           "wormhole_group": "C1", "url": "/map/system/31000001"}],
        "anoikis_classes": [{"key": "c1", "label": "C1", "x": 0, "y": 0,
                             "width": 540, "height": 360}],
        "anoikis_constellations": [],
    }
    extra_nodes = [{"id": 30000200 + i, "name": f"Test {i:02}", "x": 50, "y": 50, "security": .5} for i in range(30)]
    payload["nodes"].extend(extra_nodes)
    html = environment.get_template("map_eve_2d.html").render(
        map_payload=payload, map_data_json=json.dumps(payload))
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))

        def handle(route):
            url = urlsplit(route.request.url)
            if url.netloc != "map.test":
                route.abort()
            elif url.path == "/api/map/eve-2d/influence":
                route.fulfill(json={"date": "2026-10-09", "grouping": "coalition", "groups": [
                    {"id": "coalition:1", "entity_id": 1, "name": "Offline coalition", "color": "#60a5fa", "system_ids": [30000142, 30000144]}]})
            elif url.path == "/api/map/eve-2d/heat":
                query = parse_qs(url.query)
                route.fulfill(json={
                    "source": query["source"][0], "metric": query["metric"][0],
                    "from": "2026-10-08T00:00:00+00:00", "to": "2026-10-09T00:00:00+00:00",
                    "max_value": 100, "systems_count": 33,
                    "systems": [{"system_id": 30000142, "value": 5},
                                {"system_id": 31000001, "value": 10},
                                {"system_id": 99999999, "value": 100}] + [{"system_id": node["id"], "value": 3} for node in extra_nodes],
                })
            else:
                route.fulfill(content_type="text/html", body=html)

        page.route("**/*", handle)
        page.goto("http://map.test/map/eve-2d")
        page.locator('[data-eve2d-mode="heat"]').click()
        for source, metric in [("api", "kills"), ("total", "kills"),
                               ("total", "isk"), ("hidden", "isk"), ("hidden", "kills")]:
            page.locator('[data-heat-source="' + source + '"]').click()
            page.locator('[data-heat-metric="' + metric + '"]').click()
            expect(page.locator("#eve2dHeatStatus")).to_contain_text("33 systems")
            ranks = page.locator('#eve2dRankingList .eve2d-ranking-row')
            expect(ranks).to_have_count(20)
            expect(ranks.first.locator('a')).to_have_text('J055520')
            expect(ranks.first.locator('.eve2d-ranking-stats')).to_have_text('10 ' + ('ISK' if metric == 'isk' else 'kills'))
            expect(page.locator('#eve2dRankingMeta')).to_contain_text('Top 20 / 32')
            rank_query = parse_qs(urlsplit(ranks.first.locator('a').get_attribute('href')).query)
            assert rank_query['mode'] == [source]
            assert rank_query['datetime_from'] == ['2026-10-08T00:00']
            link = page.locator('#eve2dStaticHost a[href^="/system/31000001?"]')
            expect(link).to_have_count(1)
            expect(link.locator("title")).to_contain_text("J055520 · 10 " + ("ISK" if metric == "isk" else "kills"))
            query = parse_qs(urlsplit(link.get_attribute("href")).query)
            assert query["mode"] == [source], query
            assert query["datetime_from"] == ["2026-10-08T00:00"], query
            assert query["datetime_to"] == ["2026-10-09T00:00"], query
            # Anoikis lies to the right of New Eden: both heat circles must be drawn there.
            halos = page.locator('#eve2dStaticHost circle[cx="2320"][fill^="rgba(255,76,40,"]')
            expect(halos).to_have_count(2)
            expect(page.locator('#eve2dStaticHost a[href^="/system/30000142?"]')).to_have_count(1)
            expect(page.locator('#eve2dStaticHost a[href^="/system/99999999?"]')).to_have_count(0)
        page.locator("#eve2dInteractive").check()
        expect(page.locator("#eve2dCanvas")).to_be_visible()
        page.locator("#eve2dInteractive").uncheck()
        expect(page.locator('#eve2dStaticHost a[href^="/system/31000001?"]')).to_have_count(1)
        page.locator('[data-eve2d-mode="influence"]').click()
        expect(page.locator('#eve2dRankingTitle')).to_have_text('SOV Ranking')
        expect(page.locator('#eve2dRankingList')).to_contain_text('Offline coalition')
        expect(page.locator('#eve2dRankingMetric')).to_be_hidden()
        page.locator('[data-eve2d-mode="systems"]').click()
        expect(page.locator('#eve2dRankingPanel')).to_be_hidden()
        expect(page.locator('#eve2dStaticHost a[href^="/system/31000001?"]')).to_have_count(0)
        assert not errors, errors
        browser.close()
    print("Anoikis heatmap browser checks passed (Killboard/Total/Hidden, kills/ISK, mode switches).")


if __name__ == "__main__":
    main()
