"""Exercise the scripts in a temporary filesystem, without host mutations."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import shlex
import subprocess
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="magpie-helper-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "etc/magpie"
        self.state = self.root / "var/lib/magpie"
        self.binary = self.root / "bin/magpie"
        self.unit = self.root / "systemd/magpie.service"
        self.hook = self.root / "hooks/50-magpie"
        self.calls = self.root / "calls"
        for path in (self.unit.parent, self.hook.parent, self.binary.parent):
            path.mkdir(parents=True)

    def run_script(self, script, body):
        environment = dict(os.environ)
        environment.pop("MAGPIE_VERSION", None)
        environment.pop("ACME_EMAIL", None)
        # Tests must never accidentally execute a script under an elevated identity.
        return subprocess.run(["bash", "-c", self.script_command(script, body)],
                              text=True, capture_output=True, env=environment)

    def script_command(self, script, body):
        paths = {
            "config_dir": self.config,
            "state_dir": self.state,
            "binary_path": self.binary,
            "service_path": self.unit,
            "hook_path": self.hook,
            "calls": self.calls,
            "fixture_root": self.root,
        }
        setup = "\n".join(f"{key}={shlex.quote(str(value))}" for key, value in paths.items())
        return f"source {shlex.quote(str(ROOT / script))}\n{setup}\n" + r'''
install() {
  local -a args=()
  while (($#)); do
    case "$1" in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac
  done
  command install "${args[@]}"
}
id() { printf '999\n'; }
getent() {
  if [[ "$1" == passwd ]]; then
    printf 'magpie:x:999:999::%s:/usr/sbin/nologin\n' "$state_dir"
  else
    printf 'magpie:x:999:\n'
  fi
}
userdel() { printf 'userdel\n' >>"$calls"; }
groupdel() { printf 'groupdel\n' >>"$calls"; }
systemctl() { printf 'systemctl %s\n' "$*" >>"$calls"; }
runuser() { shift 3; "$@"; }
validate_host() { :; }
install_dependencies() { :; }
validate_network() { :; }
provision_certificate() { :; }
verify_installation() { :; }
''' + body

    def install(self, arguments="", target="--ip 203.0.113.10"):
        return self.run_script("install-magpie.sh", r'''
download_release() {
  download_dir="${fixture_root}/download"
  mkdir -p "$download_dir"
  printf '#!/usr/bin/env bash\nprintf "fixture-client-value\\n"\n' >"${download_dir}/magpie"
  release_tag=v0.1.1268
}
provision_certificate() { printf 'provision_certificate\n' >>"$calls"; }
main ''' + target + " " + arguments)

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_help_in_file_and_ssh_stdin_modes(self):
        for script in ("install-magpie.sh", "uninstall-magpie.sh"):
            result = subprocess.run(["bash", str(ROOT / script), "--help"], capture_output=True, text=True)
            self.assert_success(result)
            with (ROOT / script).open() as source:
                result = subprocess.run(["bash", "-s", "--", "--help"], stdin=source, capture_output=True, text=True)
            self.assert_success(result)
            self.assertIn("Usage:", result.stdout)
            self.assertIn("--purge" if script.startswith("un") else "--local-only", result.stdout)

    def check_ownership(self, script):
        return self.run_script(script, "check_ownership")

    def test_installer_refuses_unmanaged_or_leftover_files(self):
        self.assert_success(self.check_ownership("install-magpie.sh"))
        self.binary.write_text("leftover")
        self.assertEqual(self.check_ownership("install-magpie.sh").returncode, 1)
        self.binary.unlink()
        self.config.mkdir(parents=True)
        (self.config / "service.env").write_text("MAGPIE_WEB_KEY=foreign\n")
        result = self.check_ownership("install-magpie.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Unmanaged installation", result.stderr)
        (self.config / "service.env").write_text("# Managed by install-magpie.sh.\n")
        self.assert_success(self.check_ownership("install-magpie.sh"))
        self.unit.write_text("[Unit]\n")
        self.assertEqual(self.check_ownership("install-magpie.sh").returncode, 1)
        self.assertEqual((self.config / "service.env").read_text(), "# Managed by install-magpie.sh.\n")

    def test_uninstaller_refuses_unmanaged_or_missing_installation(self):
        result = self.check_ownership("uninstall-magpie.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("No installation", result.stderr)
        self.unit.write_text("[Unit]\n")
        self.assertEqual(self.check_ownership("uninstall-magpie.sh").returncode, 1)
        self.assertTrue(self.unit.exists())
        self.unit.write_text("# Managed by install-magpie.sh.\n")
        self.assert_success(self.check_ownership("uninstall-magpie.sh"))

    def test_local_only_dependencies_skip_snapd_and_certbot(self):
        result = self.run_script("install-magpie.sh", r'''
unset -f install_dependencies
source "$script"
apt-get() { printf 'apt-get %s\n' "$*" >>"$calls"; }
snap() { printf 'snap %s\n' "$*" >>"$calls"; }
timeout() { printf 'timeout %s\n' "$*" >>"$calls"; }
local_only=true
install_dependencies
'''.replace("$script", shlex.quote(str(ROOT / "install-magpie.sh"))))
        self.assert_success(result)
        calls = self.calls.read_text()
        self.assertIn("apt-get install", calls)
        for forbidden in ("snapd", "snap ", "certbot", "timeout"):
            self.assertNotIn(forbidden, calls)

    def test_local_only_verification_uses_trusted_forwarded_loopback_requests(self):
        web_key = "fixture-web-key"

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/v1/models":
                    status = 200 if self.headers.get("X-Forwarded-For") else 403
                elif self.path == "/api/state":
                    port = self.server.server_address[1]
                    status = 200 if self.headers.get("Cookie") == f"magpie_web_{port}={web_key}" else 401
                else:
                    status = 404
                self.send_response(status)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        self.config.mkdir(parents=True)
        (self.config / "service.env").write_text(f"MAGPIE_WEB_KEY={web_key}\n")
        body = r'''
unset -f verify_installation
source "$script"
curl() { printf 'curl %s\n' "$*" >>"$calls"; }
local_only=true
public_ip=203.0.113.10
gateway_port=PORT
web_addr=127.0.0.1:PORT
created_client_key=true
verify_installation
'''.replace("$script", shlex.quote(str(ROOT / "install-magpie.sh"))).replace("PORT", str(port))
        # Re-sourcing resets the script's paths, so restore the fixture configuration.
        result = self.run_script("install-magpie.sh", body.replace(
            "curl()", f"config_dir={shlex.quote(str(self.config))}\ncurl()"))
        self.assert_success(result)
        self.assertIn("Verified gateway access", result.stdout)
        self.assertIn(f"http://127.0.0.1:{port}/v1/models", self.calls.read_text())
        self.assertNotIn("203.0.113.10", self.calls.read_text())

    def test_uninstall_requires_typed_confirmation(self):
        self.assert_success(self.install())
        for answer in (b"yes\n", b"\x04"):
            body = self.script_command("uninstall-magpie.sh", "main")
            pid, fd = pty.fork()
            if pid == 0:
                os.execvpe("bash", ["bash", "-c", body], os.environ)
            output = b""
            os.write(fd, answer)
            while True:
                try:
                    chunk = os.read(fd, 1024)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
            _, status = os.waitpid(pid, 0)
            os.close(fd)
            self.assertEqual(os.waitstatus_to_exitcode(status), 3, output.decode())
            self.assertIn(b"Cancelled.", output)
            self.assertTrue(self.unit.exists())

    def test_purge_refuses_symlinked_data_directory(self):
        self.assert_success(self.install())
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "sentinel").write_text("keep")
        real_state = self.root / "real-state"
        self.state.rename(real_state)
        self.state.symlink_to(outside, target_is_directory=True)
        result = self.run_script("uninstall-magpie.sh", "main --yes --purge")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing to delete a symlink", result.stderr)
        self.assertEqual((outside / "sentinel").read_text(), "keep")
        self.assertNotIn("userdel", self.calls.read_text() if self.calls.exists() else "")

    def test_invalid_options_stop_before_host_operations(self):
        cases = (
            "", "--ip", "--ip 1.2.3.4 --gateway-port 80",
            "--ip 1.2.3.4 --gateway-port 65536", "--ip 1.2.3.4 --gateway-port 03425",
            "--ip 1.2.3.4 --management-port 443", "--ip 1.2.3.4 --magpie-version bad",
            "--ip 1.2.3.4 --enable-remote-management --disable-remote-management",
            "--local-only --ip 1.2.3.4", "--local-only --enable-remote-management",
            "--local-only --gateway-port 3430",
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.run_script("install-magpie.sh", "parse_args " + arguments)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertFalse(self.calls.exists())

    def test_fresh_install_keeps_management_local_and_keys_private(self):
        result = self.install()
        self.assert_success(result)
        unit = self.unit.read_text()
        self.assertIn("serve --web=127.0.0.1:3430", unit)
        self.assertIn("StandardOutput=null", unit)
        self.assertIn("User=magpie", unit)
        self.assertIn("CAP_NET_BIND_SERVICE", unit)
        self.assertEqual((self.config / "service.env").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.config / "client.key").stat().st_mode & 0o777, 0o600)
        self.assertNotIn("fixture-client-value", result.stdout + result.stderr)
        key_line = next(line for line in (self.config / "service.env").read_text().splitlines() if line.startswith("MAGPIE_WEB_KEY="))
        self.assertNotIn(key_line, result.stdout + result.stderr)
        self.assertIn("systemctl restart magpie.service", self.calls.read_text())
        self.assertIn("provision_certificate", self.calls.read_text())
        environment = (self.config / "service.env").read_text()
        self.assertIn("MAGPIE_ADDR=203.0.113.10:443\n", environment)
        self.assertIn("MAGPIE_TLS_CERT=", environment)
        self.assertNotIn("MAGPIE_TRUST_PROXY", environment)

    def test_local_only_install_stays_on_loopback_without_certificate(self):
        result = self.install(target="--local-only")
        self.assert_success(result)
        environment = (self.config / "service.env").read_text()
        self.assertIn("MAGPIE_ADDR=127.0.0.1:3425\n", environment)
        self.assertIn("MAGPIE_TRUST_PROXY=1\n", environment)
        for absent in ("MAGPIE_PUBLIC_URL", "MAGPIE_TLS_CERT", "MAGPIE_TLS_KEY"):
            self.assertNotIn(absent, environment)
        unit = self.unit.read_text()
        self.assertIn("serve --web=127.0.0.1:3430", unit)
        self.assertNotIn("CAP_NET_BIND_SERVICE", unit)
        self.assertNotIn("provision_certificate", self.calls.read_text())
        self.assertEqual((self.config / "client.key").stat().st_mode & 0o777, 0o600)
        self.assertNotIn("fixture-client-value", result.stdout + result.stderr)
        self.assertIn("127.0.0.1:3425/v1", result.stderr)

    def test_switching_to_local_only_returns_management_to_loopback(self):
        self.assert_success(self.install("--enable-remote-management"))
        key = (self.config / "service.env").read_text().split("MAGPIE_WEB_KEY=")[1].splitlines()[0]
        self.assert_success(self.install("--gateway-port 8317", target="--local-only"))
        environment = (self.config / "service.env").read_text()
        self.assertIn("MAGPIE_ADDR=127.0.0.1:8317\n", environment)
        self.assertIn("MAGPIE_HELPER_REMOTE_MANAGEMENT=false\n", environment)
        self.assertIn("MAGPIE_WEB_KEY=" + key, environment)
        self.assertIn("serve --web=127.0.0.1:3430", self.unit.read_text())

    def test_local_only_network_check_ignores_port_80_and_public_address(self):
        shell = f"source {shlex.quote(str(ROOT / 'install-magpie.sh'))}\n" + r'''
local_only=true gateway_port=3425 management_port=3430
ip() { return 1; }
systemctl() { printf '0\n'; }
ss() {
  printf '%s\n' "$*" >>"$CHECKED"
  [[ "$*" != *":80"* ]] || printf 'LISTEN 0 4096 0.0.0.0:80 0.0.0.0:* users:(("certbot",pid=7,fd=3))\n'
}
validate_network
'''
        checked = self.root / "checked"
        environment = dict(os.environ, CHECKED=str(checked))
        result = subprocess.run(["bash", "-c", shell], text=True, capture_output=True, env=environment)
        self.assert_success(result)
        self.assertNotIn("sport = :80\n", checked.read_text())
        self.assertIn("sport = :3425", checked.read_text())

    def test_reinstall_preserves_data_key_and_remote_choice(self):
        self.assert_success(self.install("--enable-remote-management"))
        key = (self.config / "service.env").read_text().split("MAGPIE_WEB_KEY=")[1].splitlines()[0]
        account = self.state / "provider-account.json"
        account.write_text("fixture account state")
        self.assert_success(self.install())
        self.assertIn("serve --web=203.0.113.10:3430", self.unit.read_text())
        self.assertIn("MAGPIE_WEB_KEY=" + key, (self.config / "service.env").read_text())
        self.assertEqual(account.read_text(), "fixture account state")
        self.assert_success(self.install("--disable-remote-management --gateway-port 8443 --management-port 3431"))
        self.assertIn("serve --web=127.0.0.1:3431", self.unit.read_text())
        self.assertNotIn("CAP_NET_BIND_SERVICE", self.unit.read_text())

    def test_reinstall_never_recreates_a_deleted_or_revoked_key(self):
        self.assert_success(self.install())
        store = self.state / "config/magpie/caller-keys.json"
        store.parent.mkdir(parents=True)
        store.write_text("[]")
        (self.config / "client.key").unlink()
        self.assert_success(self.install())
        self.assertFalse((self.config / "client.key").exists())
        self.assertEqual(store.read_text(), "[]")

    def test_retry_after_failed_initial_key_creation(self):
        result = self.run_script("install-magpie.sh", r'''
download_release() {
  download_dir="${fixture_root}/download"
  mkdir -p "$download_dir"
  printf '#!/usr/bin/env bash\nexit 42\n' >"${download_dir}/magpie"
}
main --ip 203.0.113.10
''')
        self.assertEqual(result.returncode, 42)
        self.assertEqual((self.config / "client.key").stat().st_size, 0)
        self.assert_success(self.install())
        self.assertEqual((self.config / "client.key").read_text().strip(), "fixture-client-value")

    def test_listener_inspection_failure_stops_network_validation(self):
        shell = f"source {shlex.quote(str(ROOT / 'install-magpie.sh'))}\n" + r'''
public_ip=8.8.8.8
ip() { printf '2: eth0 inet 8.8.8.8/32 scope global eth0\n'; }
systemctl() { printf '0\n'; }
ss() { return 41; }
validate_network
'''
        result = subprocess.run(["bash", "-c", shell], text=True, capture_output=True)
        self.assertEqual(result.returncode, 41, result.stdout + result.stderr)

    def test_corrupt_managed_environment_is_not_replaced(self):
        self.config.mkdir(parents=True)
        original = "# Managed by install-magpie.sh.\nMAGPIE_WEB_KEY=\n"
        (self.config / "service.env").write_text(original)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.config / "service.env").read_text(), original)
        self.assertFalse(self.binary.exists())

    def test_download_checks_digest_before_installing(self):
        payload = b"fixture executable bytes"
        tag = "v0.1.1268"
        name = "magpie-cli-linux-amd64"
        release = {"tag_name": tag, "draft": False, "prerelease": False, "assets": [{
            "name": name, "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
            "browser_download_url": f"https://github.com/yetone/magpie-releases/releases/download/{tag}/{name}",
        }]}
        (self.root / "release.json").write_text(json.dumps(release))
        (self.root / "payload").write_bytes(payload)
        harness = r'''
uname() { printf 'x86_64\n'; }
curl() {
  local output='' url=''
  while (($#)); do
    case "$1" in -o) output="$2"; shift 2 ;; *) url="$1"; shift ;; esac
  done
  if [[ "$url" == https://api.github.com/* ]]; then
    cp "${fixture_root}/release.json" "$output"
  else
    cp "${fixture_root}/payload" "$output"
  fi
}
trap cleanup EXIT
download_release
'''
        self.assert_success(self.run_script("install-magpie.sh", harness))
        (self.root / "payload").write_bytes(b"tampered bytes")
        result = self.run_script("install-magpie.sh", harness)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum mismatch", result.stderr)
        self.assertFalse(self.binary.exists())
        release["assets"][0]["digest"] = None
        (self.root / "release.json").write_text(json.dumps(release))
        self.assertNotEqual(self.run_script("install-magpie.sh", harness).returncode, 0)

    def test_uninstall_preview_is_read_only(self):
        self.assert_success(self.install())
        before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        result = self.run_script("uninstall-magpie.sh", "main --purge --dry-run")
        self.assert_success(result)
        after = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertIn("no changes made", result.stdout)

    def test_uninstall_requires_confirmation_and_preserves_data(self):
        self.assert_success(self.install())
        result = self.run_script("uninstall-magpie.sh", "main")
        self.assertEqual(result.returncode, 2)
        self.assertTrue(self.binary.exists())
        self.assert_success(self.run_script("uninstall-magpie.sh", "main --yes"))
        self.assertFalse(self.binary.exists())
        self.assertFalse(self.unit.exists())
        self.assertTrue((self.config / "service.env").exists())
        self.assertTrue(self.state.exists())
        self.assertNotIn("userdel", self.calls.read_text())

    def test_purge_removes_only_helper_data(self):
        self.assert_success(self.install())
        unrelated = self.root / "other-service"
        unrelated.write_text("keep")
        self.assert_success(self.run_script("uninstall-magpie.sh", "main --purge --yes"))
        self.assertFalse(self.config.exists())
        self.assertFalse(self.state.exists())
        self.assertEqual(unrelated.read_text(), "keep")
        self.assertIn("userdel", self.calls.read_text())


if __name__ == "__main__":
    unittest.main()
