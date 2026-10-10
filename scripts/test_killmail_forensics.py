#!/usr/bin/env python3
"""Offline Forensics tests. No production config, server or database is loaded.

Run with: python -m unittest scripts.test_killmail_forensics -v
Requires the web application's fastapi, httpx, jinja2 and psycopg2 dependencies.
"""
import importlib.util
import sys
import types
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from psycopg2.errors import QueryCanceled
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "eveosint_forensics_testapp"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "web/app")]
sys.modules[PACKAGE] = package


def stub(name, **values):
    module = types.ModuleType(f"{PACKAGE}.{name}")
    module.__dict__.update(values)
    sys.modules[module.__name__] = module
    return module


def offline_db():
    raise AssertionError("Tests must never connect to a real database")


def load(name):
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", ROOT / f"web/app/{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


stub("db", db=offline_db, t=lambda name: name)
stub("config", SESSION_COOKIE="test_session", SESSION_DAYS=1)
stub("security", pwd_context=None)
entities = load("entities")
filters = load("killmail_filters")
auth = load("auth")
templates = Jinja2Templates(directory=str(ROOT / "web/templates"))
stub("main_objects", templates=templates)
stub("layout", app_context=lambda **kwargs: {
    **kwargs, "username": kwargs["user"]["username"], "show_app_layout": True,
    "top_menu": [], "context_menu": [], "context_title": "Administration",
})
routes = load("routes_admin_killmail_forensics")

TIME = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
MONTH = date(2026, 10, 1)


def mer_row(row=3, month=MONTH):
    return (
        "loss", month, row, TIME, 30000142, "Jita", 10000002, "The Forge",
        587, "Rifter", "Frigate", 100001, "Victim Corp", None, None,
        588, 100002, "Killer Corp", None, None, None, False, 1000000, 0, None, None,
    )


class Connection:
    def __init__(self, rows=None, cancel=False):
        self.rows = list(rows or [])
        self.queries = []
        self.cancel = cancel
        self.rollbacks = 0

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        if "FROM mer.killmails m" in sql and self.cancel:
            raise QueryCanceled()

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def rollback(self):
        self.rollbacks += 1


@contextmanager
def fixture_db(conn):
    @contextmanager
    def fake_db():
        yield conn
    with patch.object(entities, "db", fake_db), \
         patch.object(entities, "_table_exists", return_value=True), \
         patch.object(entities, "_lookup_type_names", return_value={588: "Reaper"}), \
         patch.object(entities, "_lookup_system_locations", return_value={}), \
         patch.object(entities, "_lookup_entity_names", return_value={}):
        yield


def page_fixture(rows=None, limit=100):
    conn = Connection(rows if rows is not None else [mer_row(3), mer_row(2), mer_row(1)])
    with fixture_db(conn):
        return entities.get_hidden_killmails_page(limit, {
            "date_from": "2026-10-08", "date_to": "2026-10-08",
        })


class HiddenQueryTests(unittest.TestCase):
    def test_global_hidden_scope_and_composite_cursor(self):
        conn = Connection([mer_row(3), mer_row(2), mer_row(1)])
        with fixture_db(conn):
            page = entities.get_hidden_killmails_page(2, {"date_from": "2026-10-08", "date_to": "2026-10-08"})
        sql, params = next(item for item in conn.queries if "FROM mer.killmails m" in item[0])
        self.assertIn("m.resolved_km IS NULL", sql)
        self.assertIn("WHERE TRUE AND", sql)
        self.assertNotIn("victim_corporation_id = %s", sql)
        self.assertEqual(params[-1], 3)
        self.assertEqual([km["source_row"] for km in page["killmails"]], [3, 2])
        cursor = page["killmail_pagination"]
        self.assertEqual((cursor["scan_before"], cursor["scan_month"], cursor["scan_row"]), (TIME.isoformat(), MONTH.isoformat(), 2))
        self.assertEqual(page["killmails"][1]["selection_ref"], {
            "kill_datetime": TIME.isoformat(), "source_month": MONTH.isoformat(), "source_row": 2,
        })

    def test_builder_roles_exclusions_dates_and_zones_are_applied(self):
        conn = Connection()
        with fixture_db(conn), patch.object(entities, "_killmail_builder_zone_system_ids", return_value=[30000142]) as zones:
            entities.get_hidden_killmails_page(filters={
                "date_from": "2026-10-08", "date_to": "2026-10-08",
                "builder_ship_include": ["victim:587", "victim:588"],
                "builder_entity_include": ["attacker:corporation:100002"],
                "builder_entity_exclude": ["victim:alliance:900001"],
                "builder_ship_exclude": ["attacker:589"],
                "builder_zone_include": ["security:highsec"],
                "builder_zone_exclude": ["system:30000141"],
            })
        sql, params = next(item for item in conn.queries if "FROM mer.killmails m" in item[0])
        self.assertIn("m.resolved_km IS NULL", sql)
        self.assertIn("(m.victim_ship_type_id = %s OR m.victim_ship_type_id = %s)", sql)
        self.assertIn("m.killer_corporation_id = %s", sql)
        self.assertIn("NOT (m.victim_alliance_id = %s)", sql)
        self.assertIn("NOT (m.killer_ship_type_id = %s)", sql)
        self.assertIn("m.solar_system_id = ANY(%s)", sql)
        self.assertIn("NOT (m.solar_system_id = ANY(%s))", sql)
        self.assertIn("m.kill_datetime >= %s::date", sql)
        self.assertIn("INTERVAL '1 day'", sql)
        self.assertIn(100002, params)
        self.assertEqual(zones.call_count, 2)

    def test_resume_uses_all_three_keys_at_equal_timestamps(self):
        conn = Connection([mer_row(1)])
        with fixture_db(conn):
            page = entities.get_hidden_killmails_page(filters={
                "date_from": "2026-10-08", "scan_before": TIME.isoformat(),
                "scan_month": MONTH.isoformat(), "scan_row": "2",
            })
        sql, params = next(item for item in conn.queries if "FROM mer.killmails m" in item[0])
        self.assertIn("(m.kill_datetime, m.source_month, m.source_row) < (%s, %s, %s)", sql)
        self.assertEqual(params[-4:-1], [TIME, MONTH, 2])
        self.assertTrue(page["killmail_pagination"]["scan_complete"])

    def test_blocked_resume_keeps_composite_cursor_for_retry(self):
        conn = Connection(cancel=True)
        with fixture_db(conn):
            page = entities.get_hidden_killmails_page(filters={
                "date_from": "2026-10-08", "scan_before": TIME.isoformat(),
                "scan_month": MONTH.isoformat(), "scan_row": "2",
            })
        cursor = page["killmail_pagination"]
        self.assertTrue(cursor["scan_blocked"])
        self.assertTrue(cursor["scan_has_more"])
        self.assertEqual((cursor["scan_before"], cursor["scan_month"], cursor["scan_row"]), (TIME.isoformat(), MONTH.isoformat(), 2))
        self.assertGreater(conn.rollbacks, 0)

    def test_existing_corporation_scope_keeps_its_predicates(self):
        conn = Connection([mer_row(3), mer_row(2), mer_row(1)])
        with fixture_db(conn):
            rows, _ = entities._group_entity_mer_killmails(conn, "corporation", 100001, per_page=2)
        sql, params = next(item for item in conn.queries if "FROM mer.killmails m" in item[0])
        self.assertIn("m.victim_corporation_id = %s", sql)
        self.assertIn("m.killer_corporation_id = %s", sql)
        self.assertNotIn("m.resolved_km IS NULL", sql)
        self.assertEqual(params[:3], [100001, 100001, 100001])
        self.assertEqual(len(rows), 2)

    def test_unavailable_mer_filters_fail_before_any_db_access(self):
        for values in (
            {"builder_entity_include": ["victim:character:42"]},
            {"builder_entity_exclude": ["both:player:42"]},
            {"module_type_ids": [42]}, {"participation": "kills"},
            {"heat_entity_include": ["alliance:42"]},
            {"date_from": "2026-10-08", "date_to": "2026-10-01"},
        ):
            with self.subTest(values=values), self.assertRaises(entities.EntityError):
                entities.get_hidden_killmails_page(filters=values)


class ReuseTests(unittest.TestCase):
    def test_mer_search_omits_characters_but_public_search_keeps_them(self):
        data = [{"entity_type": kind, "entity_id": index} for index, kind in enumerate(("character", "corporation", "alliance"))]
        with patch.object(filters, "search_entities", return_value=data) as lookup:
            self.assertEqual(len(filters.search_killboard_filters("entity", "eve")), 3)
            self.assertTrue(lookup.call_args.kwargs["include_characters"])
            self.assertEqual([item["entity_type"] for item in filters.search_killboard_filters("entity", "eve", mer_only=True)], ["corporation", "alliance"])
            self.assertFalse(lookup.call_args.kwargs["include_characters"])

    def test_mer_autocomplete_excludes_characters_before_the_sql_limit(self):
        for query in ("eve", "100001"):
            conn = Connection()
            with fixture_db(conn):
                entities.search_entities(query, include_characters=False)
            selects = [item for item in conn.queries if "FROM entities.characters" in item[0]]
            self.assertTrue(selects)
            for sql, params in selects:
                self.assertIn("AND %s", sql)
                self.assertIn(False, params)

    def test_repeated_filters_and_cursor_survive_request_parsing(self):
        request = Request({"type": "http", "query_string": b"builder_ship_include=victim%3A587&builder_ship_include=attacker%3A588&scan_row=2"})
        parsed = filters.killmail_filters_from_request(request)
        self.assertEqual(parsed["builder_ship_include"], ["victim:587", "attacker:588"])
        self.assertEqual(parsed["scan_row"], "2")

    def test_only_admin_variant_has_selection_checkboxes(self):
        page = page_fixture()
        fragment = templates.env.get_template("entity_killmails_fragment.html")
        public = fragment.render(killmail_page=page)
        admin = fragment.render(killmail_page=page, killmail_selectable=True)
        self.assertNotIn("data-forensics-ref", public)
        self.assertIn('colspan="6"', public)
        self.assertEqual(admin.count("data-forensics-ref"), 3)
        self.assertIn('colspan="7"', admin)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)
        self.user = {"id": 7, "username": "dev", "permissions": {routes.PERMISSION}}
        self.login = patch.object(routes, "require_login", side_effect=lambda request: self.user)
        self.login.start()
        self.addCleanup(self.login.stop)

    def test_all_endpoints_require_dev_permission(self):
        for user, code in ((None, 401), ({"permissions": {"entities.view"}}, 403)):
            self.user = user
            with patch.object(routes, "get_hidden_killmails_page") as data, patch.object(routes, "search_killboard_filters") as lookup:
                for url in ("/admin/killmail-forensics/data", "/admin/killmail-forensics/search?kind=entity&q=eve"):
                    self.assertEqual(self.client.get(url).status_code, code)
                data.assert_not_called()
                lookup.assert_not_called()
                response = self.client.get("/admin/killmail-forensics", follow_redirects=False)
                self.assertEqual(response.status_code, 302)

    def test_data_returns_the_shared_hidden_table_and_cursor(self):
        page = page_fixture(limit=2)
        with patch.object(routes, "get_hidden_killmails_page", return_value=page) as data:
            response = self.client.get("/admin/killmail-forensics/data?limit=2&builder_ship_include=victim:587")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["html"].count("data-forensics-ref"), 2)
        self.assertIn('data-scan-row="2"', payload["html"])
        self.assertEqual(data.call_args.kwargs["filters"]["builder_ship_include"], ["victim:587"])
        self.assertEqual(data.call_args.kwargs["per_page"], 2)

    def test_filter_errors_and_query_timeouts_are_reported(self):
        for exception, code in ((entities.EntityError("Invalid filter"), 400), (QueryCanceled(), 503)):
            with patch.object(routes, "get_hidden_killmails_page", side_effect=exception):
                response = self.client.get("/admin/killmail-forensics/data")
                self.assertEqual(response.status_code, code)
                self.assertIn("error", response.json())

    def test_page_uses_the_admin_lookup_and_user_scoped_selection(self):
        response = self.client.get("/admin/killmail-forensics")
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-kb-search-url="/admin/killmail-forensics/search"', response.text)
        self.assertIn('data-selection-user="7"', response.text)
        self.assertIn("Analyzed killmails", response.text)


if __name__ == "__main__":
    unittest.main()
