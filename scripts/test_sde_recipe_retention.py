"""Offline checks for retaining dated recipes across normal SDE imports."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, mock_open, MagicMock, call

from scripts.killmail_recipe_history import RecipeHistory


spec = importlib.util.spec_from_file_location("scripts.sde_recipe_test", Path(__file__).with_name("sync_sde.py"))
sync = importlib.util.module_from_spec(spec)
with patch.object(Path, "open", mock_open(read_data="{}")):
    spec.loader.exec_module(sync)


class RetentionTests(unittest.TestCase):
    def test_release_date_is_taken_from_ccp_metadata(self):
        response = SimpleNamespace(status_code=200, text=json.dumps({
            "_key": "sde", "buildNumber": 123, "releaseDate": "2026-11-01T11:00:00Z",
        }), raise_for_status=lambda: None)
        conn = MagicMock()
        with patch.object(sync, "http_get_cached", return_value=response):
            self.assertEqual(sync.get_latest_build(conn), 123)
        conn.cursor.return_value.__enter__.return_value.execute.assert_called_once()
        self.assertEqual(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[1],
                         ("sde_release_date:123", "2026-11-01T11:00:00Z"))

    def test_imported_recipes_are_saved_once_without_a_second_download(self):
        rows = [{"activities": {"manufacturing": {
            "products": [{"typeID": 1, "quantity": 2}],
            "materials": [{"typeID": 2, "quantity": 10}],
        }}}]
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = ("2026-11-01T11:00:00Z",)
        cursor.__iter__.return_value = iter([(row,) for row in rows])
        with tempfile.TemporaryDirectory() as directory, patch.object(sync.requests, "get") as network:
            sync.retain_imported_recipes(conn, 123, Path(directory))
            cursor.execute.reset_mock()
            sync.retain_imported_recipes(conn, 123, Path(directory))
            cursor.execute.assert_not_called()
            network.assert_not_called()
            history = RecipeHistory(runtime_dir=directory)
            self.assertEqual(history.recipes("build-123"), {"1": [2, [[2, 10]]]})

    def test_previous_recipes_are_retained_before_new_tables_replace_them(self):
        conn = MagicMock()
        events = []
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "blueprints.jsonl").write_text("{}\n")
            with patch.object(sync, "db", return_value=conn), \
                 patch.object(sync, "ensure_base_tables"), \
                 patch.object(sync, "get_latest_build", return_value=20), \
                 patch.object(sync, "get_current_build", return_value=10), \
                 patch.object(sync, "already_imported", return_value=False), \
                 patch.object(sync, "retain_imported_recipes", side_effect=lambda _c, b: events.append(("retain", b))) as retain, \
                 patch.object(sync, "download_sde", return_value=Path(directory) / "sde.zip"), \
                 patch.object(sync, "extract_sde", return_value=Path(directory)), \
                 patch.object(sync, "import_file", side_effect=lambda *_: events.append(("replace", 20))), \
                 patch.object(sync, "rebuild_sde_work_ship_entities"), \
                 patch.object(sync, "rebuild_sde_work_type_required_skills"), patch("builtins.print"):
                sync.main()
            self.assertEqual(retain.call_args_list, [call(conn, 10), call(conn, 20)])
            self.assertEqual(events, [("retain", 10), ("replace", 20), ("retain", 20)])


if __name__ == "__main__":
    unittest.main()
