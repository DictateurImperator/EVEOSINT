"""Offline tests of shared ship groups and the global MER killboard routes."""

import ast
import logging
import unittest
from datetime import date, datetime
from unittest.mock import Mock, patch

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.testclient import TestClient
from psycopg2.errors import QueryCanceled

from scripts import test_killmail_forensics as f

OPTIONS = [
    {"entity_id": 587, "name": "Rifter", "group_name": "Frigate"},
    {"entity_id": 588, "name": "Reaper", "group_name": "Frigate"},
    {"entity_id": 589, "name": "Catalyst", "group_name": "Destroyer"},
]


class ShipGroupTests(unittest.TestCase):
    def test_search_offers_a_group_before_individual_types(self):
        with patch.object(f.filters, "get_killmail_ship_options", return_value=OPTIONS):
            results = f.filters.search_killboard_filters("ship", "frigate")
        self.assertEqual(results[0]["entity_type"], "ship_group")
        self.assertEqual(results[0]["entity_id"], "Frigate")
        self.assertEqual([r["entity_id"] for r in results[1:]], [587, 588])

    def test_group_compiles_to_all_types_on_the_requested_side(self):
        with patch.object(f.entities, "get_killmail_ship_options", return_value=OPTIONS):
            normalized = f.entities._normalize_killmail_filters({
                "builder_ship_include": ["victim:group:Frigate", "victim:group:Frigate"],
                "builder_ship_exclude": ["attacker:group:Destroyer"],
            })
        term = normalized["builder_ship_include"][0]
        self.assertEqual(len(normalized["builder_ship_include"]), 1)
        sql, values = f.entities._killmail_builder_ship_predicate(term)
        self.assertEqual(sql, "km.victim_ship_type_id = ANY(%s)")
        self.assertEqual(values, [[587, 588]])
        sql, values = f.entities._killmail_builder_ship_predicate(normalized["builder_ship_exclude"][0])
        self.assertIn("bsa.ship_type_id = ANY(%s)", sql)
        self.assertEqual(values, [[589]])

    def test_invalid_group_is_rejected(self):
        with patch.object(f.entities, "get_killmail_ship_options", return_value=OPTIONS), self.assertRaises(f.entities.EntityError):
            f.entities._normalize_killmail_filters({"builder_ship_include": ["victim:group:Missing"]})

    def test_mer_all_and_hidden_use_groups_and_composite_cursor(self):
        for hidden in (False, True):
            with self.subTest(hidden=hidden):
                conn = f.Connection([f.mer_row(3), f.mer_row(2), f.mer_row(1)])
                filters = {"date_from": "2026-10-08", "date_to": "2026-10-08",
                           "builder_ship_include": ["victim:group:Frigate"],
                           "builder_ship_exclude": ["attacker:group:Destroyer"]}
                with f.fixture_db(conn), patch.object(f.entities, "get_killmail_ship_options", return_value=OPTIONS):
                    page = f.entities.get_global_mer_killmails_scan(2, filters, hidden_only=hidden)
                sql, params = next(q for q in conn.queries if "FROM mer.killmails m" in q[0])
                self.assertEqual("m.resolved_km IS NULL" in sql, hidden)
                self.assertIn("m.victim_ship_type_id = ANY(%s)", sql)
                self.assertIn("NOT (m.killer_ship_type_id = ANY(%s))", sql)
                self.assertIn([587, 588], params)
                pagination = page["killmail_pagination"]
                self.assertEqual(pagination["scan_row"], 2)
                self.assertEqual(pagination["scan_month"], "2026-10-01")
                self.assertEqual(len(page["killmails"]), 2)
                self.assertTrue(pagination["has_next"])


def route_namespace():
    # Load the actual global route definitions without unrelated production
    # route imports/config. The application query functions are patched below.
    names = {"_killmail_filters_have_user_filters", "_global_killboard_mode", "_global_killboard_scan",
             "global_killboard", "global_killboard_fragment", "global_killboard_scan"}
    source = f.ROOT / "web/app/routes_entities.py"
    module = ast.parse(source.read_text())
    module.body = [node for node in module.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {
        "router": APIRouter(), "Request": Request, "HTMLResponse": HTMLResponse,
        "date": date, "datetime": datetime, "EntityError": f.entities.EntityError,
        "QueryCanceled": QueryCanceled, "logger": logging.getLogger(__name__),
        "require_login": lambda request: {"username": "test"},
        "require_permission_or_redirect": lambda user, permission: None,
        "app_context": lambda **kwargs: {"title": kwargs["title"]}, "templates": f.templates,
        "_killmail_filters_from_request": f.filters.killmail_filters_from_request,
        "_entity_route_error_response": lambda request, exc: Response(status_code=400),
        "get_global_mer_killmails_scan": Mock(), "get_global_killmails_scan_month": Mock(),
        "get_global_killmails_page": Mock(),
    }
    exec(compile(module, str(source), "exec"), namespace)  # noqa: S102 - trusted repository route definitions
    return namespace


class GlobalModeRouteTests(unittest.TestCase):
    def setUp(self):
        self.ns = route_namespace()
        app = FastAPI()
        app.include_router(self.ns["router"])
        self.client = TestClient(app)

    def test_all_and_hidden_ssr_defer_queries(self):
        for mode in ("total", "hidden"):
            response = self.client.get("/killboard?mode=" + mode)
            self.assertEqual(response.status_code, 200)
            self.assertIn("Preparing progressive", response.text)
        self.ns["get_global_mer_killmails_scan"].assert_not_called()
        self.ns["get_global_killmails_page"].assert_not_called()

    def test_mer_scan_headers_preserve_row_cursor(self):
        for mode in ("total", "hidden"):
            self.ns["get_global_mer_killmails_scan"].return_value = {
                "global_context": True, "killmails": [], "killmail_groups": [],
                "killmail_pagination": {"has_next": True, "scan_month": "2026-10-01", "scan_row": 2,
                                        "scan_before": "2026-10-08T12:00:00+00:00", "scan_end_label": "2026-10-08"},
            }
            response = self.client.get("/killboard/scan?mode=" + mode + "&remaining=2&builder_ship_include=victim:group:Frigate")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["X-KB-Next-Row"], "2")
            self.assertEqual(response.headers["X-KB-Next-Month"], "2026-10-01")
            self.assertEqual(response.headers["X-KB-Next-Before"], "2026-10-08T12:00:00+00:00")
            kwargs = self.ns["get_global_mer_killmails_scan"].call_args.kwargs
            self.assertEqual(kwargs["hidden_only"], mode == "hidden")
            self.assertEqual(kwargs["filters"]["builder_ship_include"], ["victim:group:Frigate"])

    def test_empty_window_preserves_timestamp_without_inventing_row_month(self):
        self.ns["get_global_mer_killmails_scan"].return_value = {
            "global_context": True, "killmails": [], "killmail_groups": [],
            "killmail_pagination": {"has_next": True, "scan_before": "2026-09-08T12:00:00+00:00",
                                    "scan_end_label": "2026-09-08", "scan_month": None, "scan_row": None},
        }
        response = self.client.get("/killboard/scan?mode=hidden")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("X-KB-Next-Month", response.headers)
        self.assertIn("X-KB-Next-Before", response.headers)
        self.assertEqual(response.headers["X-KB-Scan-Complete"], "0")

    def test_unavailable_mer_filter_explains_error_in_fragment(self):
        self.ns["get_global_mer_killmails_scan"].side_effect = f.entities.EntityError("MER data does not contain character IDs or modules.")
        response = self.client.get("/killboard/scan?mode=hidden")
        self.assertEqual(response.status_code, 200)
        self.assertIn("MER data does not contain character IDs", response.text)
        self.assertIn("alert-danger", response.text)


if __name__ == "__main__":
    unittest.main()
