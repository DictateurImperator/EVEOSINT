"""Browser checks against an offline report and mocked manual controls."""

from unittest.mock import patch
from urllib.parse import urlsplit
from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
from scripts.test_first_loss_admin import routes
from scripts.first_loss_study.model import evolution_bucket


def fixture():
    pvp = evolution_bucket()
    pvp.update(returned=20, ship_known=20, same_ship=20)
    npc = evolution_bucket()
    npc.update(
        returned=10,
        zone_known=10,
        same_zone=0,
        ship_known=10,
        same_ship=0,
        repeat_losses=10,
        zone_transitions={"HS → LS": 10},
        ship_role_transitions={"mining → combat": 10},
    )
    h = {
        "eligible": 100,
        "returned": 30,
        "pvp": 20,
        "returned_without_pvp": 10,
        "return_rate": 0.3,
        "return_interval": [0.2, 0.4],
        "evolution": {"pvp": pvp, "without_pvp": npc},
    }
    b = {
        "n": 100,
        "horizons": {str(horizon): h for horizon in (7, 30, 90)},
        "profile_counts": {"space": {"HS": 80, "LS": 20}, "ship_role": {"mining": 100}},
    }
    return {
        "id": 1,
        "status": "completed",
        "cutoff": "2026-10-11T00:00:00+00:00",
        "summary": {
            "players": 100,
            "cluster_selection": {
                "mode": "auto",
                "selected_k": 3,
                "fitting_profiles": 66,
                "selection_profiles": 17,
                "confirmation_profiles": 17,
                "population_players": 100,
                "population_stability_ari": 0.91,
                "final_silhouette": 0.55,
                "warnings": ["Example diagnostic notice."],
                "candidates": [
                    {
                        "k": 2,
                        "valid": True,
                        "silhouette_mean": 0.40,
                        "silhouette_std": 0.02,
                        "stability_ari": 0.9,
                        "min_training_fraction": 0.25,
                    },
                    {
                        "k": 3,
                        "valid": True,
                        "silhouette_mean": 0.55,
                        "silhouette_std": 0.01,
                        "stability_ari": 0.95,
                        "min_training_fraction": 0.2,
                    },
                    {"k": 4, "valid": False, "reason": "<Unavailable>"},
                ],
            },
            "overall": b,
            "years": {"2012": b},
            "clusters": {"0": b},
            "factors": {
                "30": {
                    "ship_role": [
                        {
                            "value": "mining",
                            "n": 100,
                            "rate": 0.3,
                            "year_adjusted_difference_pp": -10.0,
                            "supported_years": 1,
                        }
                    ]
                }
            },
        },
    }


def main():
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)
    user = {
        "id": 1,
        "username": "admin",
        "permissions": {"admin.jobs.view", "admin.jobs.run"},
    }
    state = {"running": False, "progress": None}
    errors = []
    with patch.object(routes, "require_login", return_value=user), patch.object(
        routes, "first_loss_progress", side_effect=lambda: state
    ), patch.object(routes, "report", return_value=fixture()), patch.object(
        routes, "run_first_loss_study"
    ) as launch, patch.object(
        routes, "stop_first_loss_study"
    ) as stop, patch.object(
        routes, "audit_log"
    ), sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))

        def handle(route):
            parts = urlsplit(route.request.url)
            if parts.netloc != "research.test":
                route.abort()
                return
            response = client.request(
                route.request.method,
                parts.path,
                content=route.request.post_data_buffer,
                headers={"content-type": route.request.headers.get("content-type", "")},
            )
            route.fulfill(
                status=response.status_code,
                content_type=response.headers.get("content-type", "text/html"),
                body=response.content,
            )

        page.route("**/*", handle)
        page.goto("http://research.test/admin/debug/first-loss")
        expect(page.locator("#fl-cohorts")).to_contain_text("30.0%")
        expect(page.locator("#fl-clusters")).to_have_value("auto")
        expect(page.locator("#fl-selection-info")).to_contain_text("Chosen: 3 groups")
        expect(page.locator("#fl-selection-info")).to_contain_text(
            "Full-population stability: 0.910"
        )
        expect(page.locator("#fl-selection-table")).to_contain_text("0.550 ± 0.010")
        expect(page.locator("#fl-selection-table .table-success")).to_contain_text(
            "Chosen"
        )
        expect(page.locator("#fl-selection-warnings")).to_contain_text(
            "Example diagnostic notice."
        )
        expect(page.locator("#fl-selection-table")).to_contain_text("<Unavailable>")
        launch.assert_not_called()
        expect(page.locator("#fl-evolution")).to_contain_text(
            "Returned without observed PvP"
        )
        expect(page.locator("#fl-evolution")).to_contain_text("HS → LS (10)")
        expect(page.locator("#fl-clusters-table")).to_contain_text("mining (100.0%)")
        page.select_option("#fl-year", "2012")
        expect(page.locator("#fl-factors").locator("..")).to_contain_text(
            "all supported years"
        )
        page.select_option("#fl-horizon", "90")
        expect(page.locator("#fl-cohorts")).to_contain_text("30.0%")
        state.update(
            running=True,
            progress={
                "phase": "followup",
                "month": "2026-08-01",
                "processed_months": 3,
                "total_months": 5,
            },
        )
        page.click("#fl-start")
        expect(page.locator("#fl-status")).to_contain_text("Months 3 / 5")
        expect(page.locator("#fl-start")).to_be_disabled()
        launch.assert_called_once_with("auto", False)
        expect(page.locator("#fl-results")).to_be_hidden()
        # Live comparison updates without reload. The server can still return
        # the previous report while the same study ID is being refitted.
        state["progress"] = {
            "phase": "selecting_profiles",
            "tested_candidates": 2,
            "total_candidates": 29,
            "candidate_k": 4,
            "cluster_selection": {
                **fixture()["summary"]["cluster_selection"],
                "selected_k": None,
            },
        }
        expect(page.locator("#fl-status")).to_contain_text(
            "Group counts 2 / 29", timeout=7000
        )
        expect(page.locator("#fl-selection-info")).to_contain_text(
            "Comparing candidates"
        )
        expect(page.locator("#fl-results")).to_be_hidden()
        page.click("#fl-stop")
        expect(page.locator("#fl-status")).to_contain_text("Stop requested")
        stop.assert_called_once()
        state.update(running=False, progress={"phase": "completed"})
        # The same run ID must show its new completed results after refitting.
        expect(page.locator("#fl-results")).to_be_visible(timeout=7000)
        expect(page.locator("#fl-selection-info")).to_contain_text("Chosen: 3 groups")
        launch.reset_mock()
        page.select_option("#fl-clusters", "3")
        page.click("#fl-start")
        expect(page.locator("#fl-results")).to_be_visible()
        launch.assert_called_once_with("3", False)
        assert not errors, errors
        browser.close()
    print("First-loss study browser checks passed.")


if __name__ == "__main__":
    main()
