"""Test model synchronization using private temporary files and local HTTP APIs."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "sync-magpie-models.py"
SPEC = importlib.util.spec_from_file_location("model_sync", SCRIPT)
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)

CONFIG = """# Keep this comment and all unrelated formatting.
config-version: 8
access:
  api-keys: [fixture-client]
server:
  host: 127.0.0.1
  port: 8317
  tls: {enable: false}
api-keys:
  openai-compatibility:
    - name: magpie
      base-url: http://127.0.0.1:3425/v1
      keys: [{api-key: fixture-upstream}]
      models:
        - name: copilot/existing
          alias: custom-alias # Preserve this alias and comment.
    - name: unrelated
      base-url: http://127.0.0.1:9999/v1
      models: [{name: other, alias: other}]
logging: true
"""


class MappingTests(unittest.TestCase):
    def test_additive_edit_preserves_aliases_comments_and_other_providers(self):
        updated, added = sync.add_mappings(CONFIG, "magpie", {"copilot/existing", "qoder/new"})
        self.assertEqual(added, ["qoder/new"])
        self.assertIn("alias: custom-alias # Preserve this alias and comment.", updated)
        self.assertIn('        - name: "qoder/new"\n', updated)
        self.assertEqual(updated.replace('        - name: "qoder/new"\n          alias: "qoder/new"\n', ""), CONFIG)
        expected = yaml.safe_load(CONFIG)
        expected["api-keys"]["openai-compatibility"][0]["models"].append({"name": "qoder/new", "alias": "qoder/new"})
        self.assertEqual(yaml.safe_load(updated), expected)
        self.assertEqual(sync.add_mappings(updated, "magpie", {"qoder/new"}), (updated, []))

    def test_flow_empty_sequences_and_unusual_names_are_valid_yaml(self):
        for flow in ("[]", '[{name: "copilot/existing", alias: "custom-alias"}]'):
            text = CONFIG.replace('      models:\n        - name: copilot/existing\n          alias: custom-alias # Preserve this alias and comment.', "      models: " + flow)
            name = 'qoder/quoted:"x"#y\\z'
            updated, _ = sync.add_mappings(text, "magpie", {name})
            models = yaml.safe_load(updated)["api-keys"]["openai-compatibility"][0]["models"]
            self.assertEqual(models[-1], {"name": name, "alias": name})
            self.assertIn("    - name: unrelated", updated)

    def test_eof_without_newline_crlf_and_unicode_comments(self):
        text = CONFIG[:CONFIG.index("    - name: unrelated")].rstrip("\n")
        for ending in ("", "\n"):
            for newline in ("\n", "\r\n"):
                with self.subTest(ending=ending, newline=newline):
                    original = ("# 注释\n" + text + ending).replace("\n", newline)
                    updated, _ = sync.add_mappings(original, "magpie", {"opencode-go/new"})
                    self.assertTrue(updated.startswith(original))
                    self.assertEqual(yaml.safe_load(updated)["api-keys"]["openai-compatibility"][0]["models"][-1]["name"], "opencode-go/new")

    def test_legacy_layout_and_client_key_are_separate_from_provider_key(self):
        data = yaml.safe_load(CONFIG)
        legacy = {"api-keys": ["fixture-client"], "openai-compatibility": data["api-keys"]["openai-compatibility"], "port": 8317}
        text = yaml.safe_dump(legacy, sort_keys=False)
        updated, added = sync.add_mappings(text, "magpie", {"qoder/new"})
        self.assertEqual(added, ["qoder/new"])
        self.assertEqual(sync.client_connection(legacy, None), ("http://127.0.0.1:8317/v1", "fixture-client"))
        self.assertEqual(sync.client_connection(data, "https://example.test/v1"), ("https://example.test/v1", "fixture-client"))
        self.assertEqual(yaml.safe_load(updated)["api-keys"], ["fixture-client"])

    def test_alias_conflict_shared_models_and_bad_yaml_fail_without_secret_output(self):
        with self.assertRaisesRegex(sync.SyncError, "conflicts"):
            sync.add_mappings(CONFIG, "magpie", {"custom-alias"})
        shared = CONFIG.replace("      models:\n", "      models: &shared\n", 1).replace("      models: [{name: other, alias: other}]", "      models: *shared")
        with self.assertRaisesRegex(sync.SyncError, "shared model references"):
            sync.add_mappings(shared, "magpie", {"qoder/new"})
        with self.assertRaises(sync.SyncError) as caught:
            sync.parse_config("bad: [fixture-secret", "magpie")
        self.assertNotIn("fixture-secret", str(caught.exception))

    def test_existing_provider_and_model_sequence_are_required(self):
        for text in (CONFIG.replace("name: magpie", "name: different"), CONFIG.replace("      models:\n        - name: copilot/existing\n          alias: custom-alias # Preserve this alias and comment.", "      models: null")):
            with self.assertRaises(sync.SyncError):
                sync.add_mappings(text, "magpie", {"qoder/new"})

    def test_tls_requires_a_certificate_matching_address(self):
        data = yaml.safe_load(CONFIG)
        data["server"]["tls"]["enable"] = True
        with self.assertRaisesRegex(sync.SyncError, "--api-base-url"):
            sync.client_connection(data, None)
        data["server"]["host"] = "203.0.113.10"
        data["server"]["port"] = 443
        self.assertEqual(sync.client_connection(data, None)[0], "https://203.0.113.10:443/v1")


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = Path(self.temporary.name) / "config.yaml"
        self.original = CONFIG.encode()
        self.updated = sync.add_mappings(CONFIG, "magpie", {"qoder/new"})[0].encode()
        self.config.write_bytes(self.original)
        self.config.chmod(0o600)

    def apply(self):
        return sync.apply_update(self.config, self.original, self.updated, "fixture.service", "http://localhost/v1", "fixture-client", {"qoder/new"})

    def test_success_keeps_private_backup_owner_and_permissions(self):
        original_stat = self.config.stat()
        with patch.object(sync, "restart") as restart, patch.object(sync, "verify_models", return_value=3):
            backup, total = self.apply()
        self.assertEqual(total, 3)
        self.assertEqual(backup.read_bytes(), self.original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.config.read_bytes(), self.updated)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.config.stat().st_uid, self.config.stat().st_gid), (original_stat.st_uid, original_stat.st_gid))
        restart.assert_called_once_with("fixture.service")

    def test_verification_failure_restores_original_and_restarts(self):
        with patch.object(sync, "restart") as restart, patch.object(sync, "verify_models", side_effect=sync.SyncError("missing models")), self.assertRaisesRegex(sync.SyncError, "Original configuration restored"):
            self.apply()
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(restart.call_count, 2)

    def test_preparation_and_verification_do_not_overwrite_concurrent_edits(self):
        self.config.write_bytes(b"concurrent change")
        with patch.object(sync, "restart") as restart, self.assertRaisesRegex(sync.SyncError, "changed during preparation"):
            self.apply()
        restart.assert_not_called()
        self.assertEqual(self.config.read_bytes(), b"concurrent change")
        self.config.write_bytes(self.original)
        def concurrent_verification(*args):
            self.config.write_bytes(b"later concurrent change")
            return 3
        with patch.object(sync, "restart"), patch.object(sync, "verify_models", side_effect=concurrent_verification), self.assertRaisesRegex(sync.SyncError, "Concurrent edit left in place"):
            self.apply()
        self.assertEqual(self.config.read_bytes(), b"later concurrent change")

    def test_world_readable_config_and_symlinks_are_not_modified(self):
        self.config.chmod(0o644)
        with self.assertRaisesRegex(sync.SyncError, "0600"):
            self.apply()
        self.assertEqual(self.config.read_bytes(), self.original)
        link = self.config.parent / "link.yaml"
        link.symlink_to(self.config)
        with self.assertRaises(sync.SyncError):
            sync.apply_update(link, self.original, self.updated, "fixture.service", "unused", "unused", set())

    def test_edits_during_update_or_recovery_temp_preparation_are_preserved(self):
        for interrupt_at in (2, 3):
            with self.subTest(interrupt_at=interrupt_at):
                self.config.write_bytes(self.original)
                concurrent = b"fixture: concurrent edit\n"
                real_fsync = os.fsync
                calls = []

                def fsync_with_editor(fd, sync_call=real_fsync, seen=calls, at=interrupt_at, content=concurrent):
                    sync_call(fd)
                    seen.append(fd)
                    if len(seen) == at:
                        self.config.write_bytes(content)

                verification = {"return_value": 3} if interrupt_at == 2 else {"side_effect": sync.SyncError("missing models")}
                with patch.object(sync.os, "fsync", side_effect=fsync_with_editor), patch.object(sync, "restart") as restart, patch.object(sync, "verify_models", **verification), self.assertRaises(sync.SyncError):
                    self.apply()
                self.assertEqual(self.config.read_bytes(), concurrent)
                self.assertEqual(restart.call_count, 0 if interrupt_at == 2 else 1)

    def test_systemctl_unit_is_not_interpreted_as_an_option(self):
        with patch.object(sync.subprocess, "run") as run:
            sync.restart("--all")
        self.assertEqual(run.call_args.args[0], ["systemctl", "restart", "--", "--all"])


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "config.yaml"
        self.loaded = self.root / "loaded.yaml"
        self.calls = self.root / "calls"
        self.source = {"copilot/existing", "qoder/new", "opencode-go/new", "group/skip"}
        self.failure = None
        self.redirects = 0
        case = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/capture":
                    case.redirects += 1
                upstream = self.path == "/magpie/v1/models"
                expected_key = "fixture-upstream" if upstream else "fixture-client"
                if self.headers.get("Authorization") != "Bearer " + expected_key:
                    self.send_response(401)
                    self.end_headers()
                    return
                if upstream and case.failure:
                    self.send_response(case.failure)
                    if case.failure == 302:
                        self.send_header("Location", "/capture")
                    self.end_headers()
                    self.wfile.write(b"fixture-secret-error-body")
                    return
                if upstream:
                    ids = case.source
                else:
                    config = yaml.safe_load(case.loaded.read_text())
                    ids = {"native-model"} | {model.get("alias") or model["name"] for provider in config["api-keys"]["openai-compatibility"] for model in provider["models"]}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"data": [{"id": name} for name in sorted(ids)]}).encode())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_address[1]}"
        text = CONFIG.replace("http://127.0.0.1:3425/v1", self.base + "/magpie/v1")
        self.config.write_text(text)
        self.config.chmod(0o600)
        self.loaded.write_text(text)
        binary = self.root / "bin"
        binary.mkdir()
        systemctl = binary / "systemctl"
        systemctl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$FIXTURE_CALLS"\ncp "$FIXTURE_CONFIG" "$FIXTURE_LOADED"\n')
        systemctl.chmod(0o755)
        self.environment = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ.get("PATH", ""), FIXTURE_CALLS=str(self.calls), FIXTURE_CONFIG=str(self.config), FIXTURE_LOADED=str(self.loaded))

    def command(self, *arguments, root=True):
        wrapper = "import os,runpy,sys; os.geteuid=lambda: " + ("0" if root else "1") + "; sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')"
        return subprocess.run([sys.executable, "-c", wrapper, str(SCRIPT), "--config", str(self.config), "--api-base-url", self.base + "/cli/v1", "--json", *arguments], check=False, capture_output=True, text=True, env=self.environment)

    def test_preview_apply_and_repeat_use_actual_apis_without_real_host_changes(self):
        before = self.config.read_bytes()
        arguments = ("--prefix", "qoder/", "--prefix", "opencode-go/")
        preview = self.command(*arguments)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertEqual(json.loads(preview.stdout)["added"], ["opencode-go/new", "qoder/new"])
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.calls.exists())
        self.assertEqual(list(self.root.glob("config.yaml.bak-*")), [])
        applied = self.command(*arguments, "--apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        result = json.loads(applied.stdout)
        self.assertTrue(result["applied"])
        self.assertEqual(result["total"], 5)
        self.assertEqual(Path(result["backup"]).read_bytes(), before)
        repeat = self.command(*arguments, "--apply")
        self.assertEqual(repeat.returncode, 0, repeat.stderr)
        self.assertEqual(json.loads(repeat.stdout)["added"], [])
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.assertEqual(len(list(self.root.glob("config.yaml.bak-*"))), 1)

    def test_http_errors_and_redirects_do_not_disclose_or_forward_credentials(self):
        for code in (500, 302):
            self.failure = code
            result = self.command()
            self.assertEqual(result.returncode, 1)
            self.assertIn(f"HTTP {code}", result.stderr)
            self.assertNotIn("fixture-secret", result.stdout + result.stderr)
            self.assertNotIn("fixture-upstream", result.stdout + result.stderr)
        self.assertEqual(self.redirects, 0)
        self.assertFalse(self.calls.exists())

    def test_apply_requires_root_and_help_works_via_stdin(self):
        result = self.command("--apply", root=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("root", result.stderr)
        result = subprocess.run([sys.executable, "-", "--help"], check=False, input=SCRIPT.read_text(), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--apply", result.stdout)


if __name__ == "__main__":
    unittest.main()
