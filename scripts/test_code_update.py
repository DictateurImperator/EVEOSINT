"""Offline checks for named Git deployment targets; no services or database."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from web.app import code_update


class BranchDiscoveryTests(unittest.TestCase):
    def test_numeric_sort_and_stable_groups(self):
        names = ["main", "dev_0.1.2_009", "stable/0.1.1", "dev_0.1.2_100",
                 "stable/0.1.2", "dev_0.1.1_025", "dev_0.1.2_041"]
        output = "\n".join(f"{'a' * 40}\trefs/heads/{name}" for name in names)
        with patch.object(code_update, "_configured_origin", return_value="local"), \
             patch.object(code_update, "_configured_branch", return_value="main"), \
             patch.object(code_update, "_git", return_value=SimpleNamespace(returncode=0, stdout=output)):
            targets = code_update._discover_remote_targets()
        self.assertEqual([target["label"] for target in targets], [
            "dev_0.1.2_100", "dev_0.1.2_041", "dev_0.1.2_009", "stable/0.1.2",
            "dev_0.1.1_025", "stable/0.1.1", "main",
        ])
        groups = code_update._group_branch_targets(targets)
        self.assertEqual([group["label"] for group in groups], [
            "Based on stable/0.1.2", "Based on stable/0.1.1", "Other branches",
        ])
        self.assertEqual(groups[0]["items"][-1]["label"], "stable/0.1.2")
        self.assertEqual(code_update._target_by_key("latest", targets)["label"], "main")
        self.assertEqual(code_update._target_by_key("stable/0.1.2", targets)["label"], "stable/0.1.2")

    def test_configured_branch_and_branch_named_latest_have_distinct_keys(self):
        output = "\n".join(f"{'a' * 40}\trefs/heads/{name}" for name in ("main", "latest"))
        with patch.object(code_update, "_configured_origin", return_value="local"), \
             patch.object(code_update, "_configured_branch", return_value="main"), \
             patch.object(code_update, "_git", return_value=SimpleNamespace(returncode=0, stdout=output)):
            targets = code_update._discover_remote_targets()
        self.assertEqual(len({target["key"] for target in targets}), 2)


class BranchFetchTests(unittest.TestCase):
    def test_branch_selection_deployment_and_failure_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, remote, client = root / "source", root / "remote.git", root / "client"

            def git(cwd, *args):
                return subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()

            git(root, "init", "-b", "main", str(source))
            (source / "example.txt").write_text("base")
            git(source, "add", "example.txt")
            git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "base")
            base = git(source, "rev-parse", "HEAD")
            git(root, "clone", "--bare", str(source), str(remote))
            git(root, "clone", str(remote), str(client))
            git(source, "switch", "-c", "dev_0.1.2_041")
            (source / "example.txt").write_text("dev")
            git(source, "add", "example.txt")
            git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "dev")
            dev = git(source, "rev-parse", "HEAD")
            git(source, "push", str(remote), "dev_0.1.2_041")
            git(source, "switch", "-c", "dev_0.1.2_042")
            (source / "example.txt").write_text("next dev")
            git(source, "add", "example.txt")
            git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "next dev")
            git(source, "push", str(remote), "dev_0.1.2_042")
            git(source, "switch", "-c", "feature/unrelated", base)
            (source / "example.txt").write_text("unrelated")
            git(source, "add", "example.txt")
            git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "unrelated")
            unrelated_sha = git(source, "rev-parse", "HEAD")
            git(source, "push", str(remote), "feature/unrelated")

            run = root / "run"
            original_run = code_update._run

            def isolated_run(command, **kwargs):
                if command[0] == "sudo":
                    return SimpleNamespace(returncode=0)
                return original_run(command, cwd=kwargs.pop("cwd", client), **kwargs)

            with patch.object(code_update, "REPO_ROOT", client), \
                 patch.object(code_update, "RUN_DIR", run), \
                 patch.object(code_update, "STATUS_FILE", run / "status.json"), \
                 patch.object(code_update, "HISTORY_FILE", run / "history.jsonl"), \
                 patch.object(code_update, "REQUEST_FILE", run / "request.json"), \
                 patch.object(code_update, "LOCK_FILE", run / "update.lock"), \
                 patch.object(code_update, "load_git_settings", return_value={"origin_url": str(remote), "branch": "main"}), \
                 patch.object(code_update, "_run", side_effect=isolated_run), \
                 patch.object(code_update, "_service_running", return_value=False):
                self.assertFalse(code_update._commit_exists(dev))
                self.assertEqual(code_update.refresh_remote(), base)
                snapshot = code_update.get_code_update_snapshot()
                self.assertEqual(snapshot["state"], "update_available")
                self.assertTrue(snapshot["update_allowed"])
                self.assertEqual(snapshot["behind"], 0)
                self.assertEqual({target["label"] for target in snapshot["update_targets"]},
                                 {"dev_0.1.2_041", "dev_0.1.2_042", "feature/unrelated"})
                target, fetched = code_update._fetch_remote_target("refs/heads/dev_0.1.2_041")
                self.assertEqual(fetched, dev)
                git(client, "reset", "--hard", dev)
                code_update._write_status(target_ref=target["key"])
                snapshot = code_update.get_code_update_snapshot()
                self.assertEqual(snapshot["current_version"], "dev_0.1.2_041")
                self.assertEqual(snapshot["state"], "update_available")
                self.assertEqual([item["label"] for item in snapshot["update_targets"]], ["dev_0.1.2_042"])
                self.assertTrue(snapshot["deploy_allowed"])
                unrelated = next(item for item in snapshot["remote_targets"] if item["label"] == "feature/unrelated")
                self.assertFalse(unrelated["can_update"])
                self.assertFalse(unrelated["can_rollback"])
                self.assertTrue(unrelated["can_deploy"])
                self.assertIn("main", {item["label"] for item in snapshot["rollback_targets"]})

                with patch.object(code_update, "_run_post_checkout_steps"), \
                     patch.object(code_update, "_restart_web_service"), \
                     patch.object(code_update, "_health_check", return_value=True):
                    code_update.queue_code_action("deploy", unrelated["key"])
                    code_update.run_pending_request()
                    self.assertEqual(git(client, "rev-parse", "HEAD"), unrelated_sha)
                    self.assertEqual((client / "example.txt").read_text(), "unrelated")
                    self.assertEqual(code_update.get_code_update_snapshot()["last_result"], "success")
                    with self.assertRaisesRegex(code_update.CodeUpdateError, "target_not_forward"):
                        code_update._perform_action("update", target["key"])

                    with patch.object(code_update, "_health_check", side_effect=[
                        code_update.CodeUpdateError("health_check_failed"), True,
                    ]):
                        with self.assertRaisesRegex(code_update.CodeUpdateError, "deploy_failed_rolled_back"):
                            code_update._perform_action("deploy", target["key"])
                    self.assertEqual(git(client, "rev-parse", "HEAD"), unrelated_sha)
                    self.assertEqual((client / "example.txt").read_text(), "unrelated")
                    self.assertEqual(code_update.get_code_update_snapshot()["last_result"], "failed")

                    code_update.queue_code_action("deploy", "latest")
                    code_update.run_pending_request()
                    self.assertEqual(git(client, "rev-parse", "HEAD"), base)
                    self.assertEqual((client / "example.txt").read_text(), "base")
                    with self.assertRaisesRegex(code_update.CodeUpdateError, "target_is_current"):
                        code_update._perform_action("deploy", "latest")

                with self.assertRaisesRegex(code_update.CodeUpdateError, "target_not_found"):
                    code_update.queue_code_action("deploy", "refs/heads/missing")
                with patch.object(code_update, "_service_running", return_value=True):
                    with self.assertRaisesRegex(code_update.CodeUpdateError, "update_already_running"):
                        code_update.queue_code_action("deploy", target["key"])


if __name__ == "__main__":
    unittest.main()
