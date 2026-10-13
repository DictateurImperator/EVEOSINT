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

    def double_counts(value):
        if isinstance(value, dict):
            return {key: double_counts(item) for key, item in value.items()}
        if isinstance(value, list):
            return [double_counts(item) for item in value]
        return value * 2 if isinstance(value, int) else value

    overall = double_counts(b)

    def pca(players, ratios, feature):
        cumulative = 0
        spectrum = []
        for component, ratio in enumerate(ratios, 1):
            cumulative += ratio
            spectrum.append(
                {
                    "component": component,
                    "variance_ratio": ratio,
                    "cumulative_ratio": cumulative,
                    "retained": component <= 3,
                    "top_features": [
                        {
                            "name": feature,
                            "coefficient": 0.8,
                            "squared_coefficient": 0.64,
                        }
                    ],
                }
            )
        return dict(
            players=players,
            dimensions=4,
            components_retained=3,
            retained_variance_ratio=sum(ratios[:3]),
            spectrum=spectrum,
        )

    annual = {}
    for year, rate, other_rate in [("2012", 0.2, 0.4), ("2026", 0.4, 0.2)]:
        annual[year] = {
            "ship_role": [
                dict(
                    value="mining",
                    n=50,
                    returned=int(50 * rate),
                    rate=rate,
                    interval=[rate - 0.05, rate + 0.05],
                    other_n=50,
                    other_rate=other_rate,
                    other_interval=[other_rate - 0.05, other_rate + 0.05],
                    difference_pp=100 * (rate - other_rate),
                )
            ]
        }
    return {
        "id": 1,
        "status": "completed",
        "cutoff": "2026-10-11T00:00:00+00:00",
        "summary": {
            "players": 200,
            "pca": {
                "coordinate_dimensions": 4,
                "global": pca(200, [0.4, 0.3, 0.2, 0.1], "corp_members"),
                "years": {
                    "2012": pca(100, [0.6, 0.2, 0.1, 0.1], "ship_role=mining"),
                    "2026": pca(100, [0.5, 0.25, 0.2, 0.05], "age_days"),
                },
            },
            "factors_by_year": {str(horizon): annual for horizon in (7, 30, 90)},
            "cluster_selection": {
                "mode": "auto",
                "selected_k": 3,
                "fitting_profiles": 66,
                "selection_profiles": 17,
                "confirmation_profiles": 17,
                "population_players": 200,
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
            "overall": overall,
            "years": {"2012": b, "2026": b},
            "clusters": {"0": overall},
            "factors": {
                "30": {
                    "ship_role": [
                        {
                            "value": "mining",
                            "n": 200,
                            "rate": 0.3,
                            "year_adjusted_difference_pp": -10.0,
                            "supported_years": 1,
                        }
                    ]
                }
            },
        },
    }


def factor_fixture():
    report = fixture()
    report["id"] = 3
    report["update_token"] = "2026-10-12T21:00:00+00:00"
    s = report["summary"]
    s["data_quality"] = dict(invalid_birthdays=10)
    model = dict(
        training_n=160,
        test_n=40,
        brier=0.12,
        baseline_brier=0.24,
        brier_skill=0.5,
        auc=0.75,
        warnings=[],
        effects=[
            dict(
                factor="age",
                profile="1-5 years",
                reference="<7 days",
                effect_pp=-18.5,
                interval_pp=[-22, -15],
            ),
            dict(
                factor="ship_role",
                profile="mining",
                reference="combat",
                effect_pp=-0.5,
                interval_pp=[-3, 2],
            ),
        ],
        importance=[
            dict(
                factor="age",
                importance_percent=70,
                delta_brier=0.05,
                interval=[0.04, 0.06],
            ),
            dict(
                factor="ship_role",
                importance_percent=30,
                delta_brier=0.02,
                interval=[0.01, 0.03],
            ),
        ],
    )
    s["return_model"] = dict(
        scopes={
            name: {
                str(h): {**model, "years": {year: model for year in ("2012", "2026")}}
                for h in (7, 30, 90)
            }
            for name in ("all", "pvp", "npc")
        }
    )
    s["pca"]["global"]["variables"] = [
        dict(name="age_days", correlations=[0.8, 0.2, 0.1, 0])
    ]
    stat = dict(
        n=200,
        returned=60,
        axes=[
            dict(component=i, correlation=-0.4, returned_mean=-0.5, no_return_mean=0.5)
            for i in range(1, 5)
        ],
    )
    s["pca_return"] = dict(
        scopes={
            name: {
                str(h): {year: stat for year in ("all", "2012", "2026")}
                for h in (7, 30, 90)
            }
            for name in ("all", "pvp", "npc")
        },
        sample=[
            dict(
                character_id=1,
                year="2012",
                coordinates=[1, 1, 0, 0],
                initial_pvp=True,
                opponent="players",
                ship_role="mining",
                outcomes={
                    str(h): dict(complete=True, returned=True) for h in (7, 30, 90)
                },
            ),
            dict(
                character_id=2,
                year="2026",
                coordinates=[-1, -1, 0, 0],
                initial_pvp=False,
                opponent="NPC/object",
                ship_role="combat",
                outcomes={
                    str(h): dict(complete=True, returned=False) for h in (7, 30, 90)
                },
            ),
        ],
    )
    return report


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
    purge_state = {"running": False, "progress": None}
    errors = []
    with patch.object(routes, "require_login", return_value=user), patch.object(
        routes, "first_loss_progress", side_effect=lambda: state
    ), patch.object(routes, "report", return_value=fixture()), patch.object(
        routes, "run_first_loss_study"
    ) as launch, patch.object(
        routes, "stop_first_loss_study"
    ) as stop, patch.object(
        routes, "audit_log"
    ), patch.object(
        routes, "run_first_loss_purge"
    ) as purge, patch.object(
        routes, "first_loss_purge_progress", side_effect=lambda: purge_state
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
                parts.path + ("?" + parts.query if parts.query else ""),
                content=route.request.post_data_buffer,
                headers={"content-type": route.request.headers.get("content-type", "")},
            )
            route.fulfill(
                status=response.status_code,
                content_type=response.headers.get("content-type", "text/html"),
                body=response.content,
                headers=dict(response.headers),
            )

        page.route("**/*", handle)
        page.goto("http://research.test/admin/report/first-loss")
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
        expect(page.locator("#fl-evolution")).to_contain_text("HS → LS (20)")
        expect(page.locator("#fl-pca-info")).to_contain_text("3 axes retain 90.0%")
        expect(page.locator("#fl-pca-info")).to_contain_text("4 global coordinates")
        expect(page.locator("summary").filter(has_text="All axes")).to_be_visible()
        expect(page.locator("#fl-pca-table tr")).to_have_count(4)
        expect(page.locator("#fl-pca-table tr").nth(1)).to_contain_text("70.0%")
        expect(page.locator("#fl-pca-table tr").nth(2)).to_contain_text("90.0%")
        expect(page.locator("#fl-trend-factor")).to_have_value("ship_role")
        expect(page.locator("#fl-trend-profile")).to_have_value("mining")
        expect(page.locator("#fl-trend-table tr").nth(0)).to_contain_text("-20.0 pp")
        expect(page.locator("#fl-trend-table tr").nth(1)).to_contain_text("+20.0 pp")
        expect(page.locator("#fl-trend-chart circle")).to_have_count(2)
        # No connecting line across unobserved years between 2012 and 2026.
        expect(page.locator("#fl-trend-chart line")).to_have_count(1)
        expect(page.locator("#fl-clusters-table")).to_contain_text("mining (100.0%)")
        page.select_option("#fl-year", "2012")
        expect(page.locator("#fl-factor-scope")).to_contain_text("within 2012 only")
        expect(page.locator("#fl-factors")).to_contain_text("-20.0 pp")
        expect(page.locator("#fl-pca-info")).to_contain_text("Diagnostic PCA · 2012")
        expect(page.locator("#fl-pca-table")).to_contain_text("ship_role=mining")
        page.select_option("#fl-year", "2026")
        expect(page.locator("#fl-factors")).to_contain_text("+20.0 pp")
        expect(page.locator("#fl-pca-table")).to_contain_text("age_days")
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
        # Older reports remain readable without silently substituting the
        # pooled association for a requested yearly comparison.
        legacy = fixture()
        legacy["id"] = 2
        legacy["summary"].pop("pca")
        legacy["summary"].pop("factors_by_year")
        routes.report.return_value = legacy
        page.reload()
        expect(page.locator("#fl-pca-info")).to_contain_text("unavailable")
        page.select_option("#fl-year", "2012")
        expect(page.locator("#fl-factors")).to_contain_text(
            "yearly comparisons unavailable"
        )
        routes.report.return_value = factor_fixture()
        page.reload()
        expect(page.locator("#fl-scope")).to_have_value("pvp")
        expect(page.locator("#fl-model-effects")).to_contain_text("-18.5 pp")
        expect(page.locator("#fl-model-effects")).to_contain_text(
            "Negative association"
        )
        expect(page.locator("#fl-model-effects")).to_contain_text("Uncertain")
        expect(page.locator("#fl-model-importance")).to_contain_text("70.0%")
        expect(page.locator("#fl-correlation-circle circle")).to_have_count(2)
        expect(page.locator("#fl-character-plane circle")).to_have_count(3)
        expect(page.locator("#fl-axis-return tr")).to_have_count(4)
        page.select_option("#fl-axis-x", "2")
        expect(page.locator("#fl-axis-y")).to_have_value("3")
        page.select_option("#fl-year", "2026")
        page.select_option("#fl-horizon", "90")
        page.select_option("#fl-scope", "npc")
        page.select_option("#fl-pdf-scope", "view")
        expect(page.locator("#fl-pdf")).to_have_attribute(
            "href", "/admin/report/first-loss/report.pdf?horizon=90&year=2026&scope=npc"
        )
        with page.expect_download() as download:
            page.click("#fl-pdf")
        assert download.value.suggested_filename.endswith("_90_2026_npc.pdf")
        # A lightweight unchanged response keeps the already loaded report.
        routes.report.return_value = {
            **factor_fixture(),
            "summary": None,
            "unchanged": True,
        }
        expect(page.locator("#fl-model-effects")).to_contain_text(
            "-18.5 pp", timeout=7000
        )
        page.wait_for_timeout(5200)
        expect(page.locator("#fl-selection")).to_be_visible()
        expect(page.locator("#fl-model-effects")).to_contain_text("-18.5 pp")
        # Same ID with a new completed revision must replace cached results.
        changed = factor_fixture()
        changed["update_token"] = "2026-10-12T22:00:00+00:00"
        changed["summary"]["return_model"]["scopes"]["pvp"]["30"]["effects"][0][
            "effect_pp"
        ] = -12
        routes.report.return_value = changed
        expect(page.locator("#fl-model-effects")).to_contain_text(
            "-12.0 pp", timeout=7000
        )
        # Purge is manual, requires confirmation, and blocks study starts while running.
        purge.assert_not_called()
        page.once("dialog", lambda dialog: dialog.dismiss())
        page.click("#fl-purge")
        purge.assert_not_called()
        page.once("dialog", lambda dialog: dialog.accept())
        page.click("#fl-purge")
        expect(page.locator("#fl-purge")).to_be_enabled()
        purge.assert_called_once()
        purge_state.update(running=True, progress={"phase": "running"})
        expect(page.locator("#fl-start")).to_be_disabled(timeout=7000)
        expect(page.locator("#fl-rebuild")).to_be_disabled()
        expect(page.locator("#fl-purge")).to_be_disabled()
        expect(page.locator("#fl-purge-status")).to_contain_text("running")
        purge_state.update(running=False, progress={"phase": "completed"})
        routes.report.return_value = None
        state["progress"] = {"phase": "purged"}
        expect(page.locator("#fl-results")).to_be_hidden(timeout=7000)
        expect(page.locator("#fl-start")).to_be_enabled()
        expect(page.locator("#fl-status")).to_contain_text("purged")
        assert not errors, errors
        browser.close()
    print("First-loss study browser checks passed.")


if __name__ == "__main__":
    main()
