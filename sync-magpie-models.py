#!/usr/bin/env python3
"""Add missing Magpie model mappings to an existing CLIProxyAPI provider."""

import argparse
import copy
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class SyncError(Exception):
    """An actionable error that does not contain credentials or response bodies."""


def yaml_module():
    try:
        import yaml
    except ImportError:
        raise SyncError("Install PyYAML first: apt-get install python3-yaml") from None
    return yaml


def field(node, name):
    if node.id != "mapping":
        raise SyncError("Expected a YAML mapping while locating the provider.")
    matches = [value for key, value in node.value if key.value == name]
    if len(matches) != 1:
        raise SyncError(f"Expected exactly one YAML field named {name!r}.")
    return matches[0]


def check_models(provider):
    models = provider.get("models")
    if not isinstance(models, list) or any(
        not isinstance(model, dict) or not isinstance(model.get("name"), str)
        or not isinstance(model.get("alias", model["name"]), str) for model in models
    ):
        raise SyncError("The provider must have a models list; use models: [] to start empty.")


def parse_config(text, provider_name):
    yaml = yaml_module()
    try:
        data = yaml.safe_load(text)
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        raise SyncError("Invalid YAML configuration; fix it before syncing.") from None
    if not isinstance(data, dict):
        raise SyncError("CLIProxyAPI configuration must be a YAML mapping.")
    nested = data.get("config-version") == 8
    owner = data.get("api-keys") if nested else data
    owner_node = field(root, "api-keys") if nested else root
    providers = owner.get("openai-compatibility", []) if isinstance(owner, dict) else []
    if not isinstance(providers, list):
        raise SyncError("openai-compatibility must be a provider list.")
    matches = [i for i, value in enumerate(providers) if isinstance(value, dict) and value.get("name") == provider_name]
    if len(matches) != 1:
        raise SyncError("Configure exactly one named Magpie provider before syncing.")
    provider = providers[matches[0]]
    check_models(provider)
    if provider.get("prefix"):
        raise SyncError("Use full model aliases instead of a provider-level prefix before syncing.")
    node = field(field(owner_node, "openai-compatibility").value[matches[0]], "models")
    return data, provider, node


def insert_models(text, node, additions):
    if node.flow_style:
        position = node.end_mark.index - 1
        insertion = (", " if node.value else "") + ", ".join(json.dumps({"name": name, "alias": name}) for name in additions)
    else:
        position = node.end_mark.index
        if position != len(text):
            position = text.rfind("\n", 0, position) + 1
        newline = "\r\n" if "\r\n" in text else "\n"
        indent = " " * (node.value[0].start_mark.column - 2)
        insertion = "".join(
            f"{indent}- name: {json.dumps(name)}{newline}"
            f"{indent}  alias: {json.dumps(name)}{newline}" for name in additions
        )
        if position and text[position - 1] != "\n":
            insertion = newline + insertion
    return text[:position] + insertion + text[position:]


def add_mappings(text, provider_name, ids):
    data, provider, node = parse_config(text, provider_name)
    existing = {model["name"] for model in provider["models"]}
    additions = sorted(set(ids) - existing)
    if not additions:
        return text, additions
    aliases = {model.get("alias") or model["name"] for model in provider["models"]}
    if aliases.intersection(additions):
        raise SyncError("A new model name conflicts with an existing alias; resolve that mapping first.")
    models = [{"name": name, "alias": name} for name in additions]
    updated = insert_models(text, node, additions)
    expected = copy.deepcopy(data)
    owner = expected["api-keys"] if data.get("config-version") == 8 else expected
    target = next(value for value in owner["openai-compatibility"] if value.get("name") == provider_name)
    target["models"] = [*target["models"], *models]
    try:
        actual = yaml_module().safe_load(updated)
    except yaml_module().YAMLError:
        raise SyncError("Cannot safely extend this YAML layout; expand model references first.") from None
    if actual != expected:
        raise SyncError("Cannot edit this YAML layout independently; expand shared model references first.")
    return updated, additions


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def check_url(base_url):
    if not isinstance(base_url, str):
        raise SyncError("Configure a model API base URL ending in /v1.")
    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise SyncError("API base URLs must be HTTP(S), without embedded credentials, queries, or fragments.")


def model_ids(base_url, key=None):
    check_url(base_url)
    headers = {"Authorization": "Bearer " + key} if key else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(base_url.rstrip("/") + "/models", headers=headers)
    try:
        with opener.open(request, timeout=10) as response:
            payload = json.load(response)
        rows = payload["data"]
        if not isinstance(rows, list) or any(not isinstance(row.get("id"), str) or not row["id"] for row in rows):
            raise ValueError
        return {row["id"] for row in rows}
    except urllib.error.HTTPError as error:
        raise SyncError(f"Model API returned HTTP {error.code}; check its URL and configured key.") from None
    except (urllib.error.URLError, OSError):
        raise SyncError("Cannot reach the model API; check its listener and TLS certificate.") from None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise SyncError("Model API response must contain data entries with nonempty string IDs.") from None


def client_connection(data, override):
    nested = data.get("config-version") == 8
    keys = data.get("access", {}).get("api-keys", []) if nested else data.get("api-keys", [])
    if not isinstance(keys, list) or not keys or not isinstance(keys[0], str) or not keys[0]:
        raise SyncError("Configure a client key in access.api-keys (v8) or api-keys (legacy).")
    if override:
        return override, keys[0]
    server = data.get("server", {}) if nested else data
    tls = server.get("tls", {}).get("enable", False)
    host = server.get("host") or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    if tls and host == "127.0.0.1":
        raise SyncError("Set --api-base-url to an HTTPS address matching the server certificate.")
    host = f"[{host}]" if ":" in host else host
    return f"{'https' if tls else 'http'}://{host}:{server.get('port', 8317)}/v1", keys[0]


def replace_config(path, content, metadata, expected):
    fd, temporary = tempfile.mkstemp(prefix=".magpie-models-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(metadata.st_mode))
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_bytes() != expected:
            raise SyncError("Configuration changed during preparation; no replacement made.")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def restart(service):
    try:
        subprocess.run(["systemctl", "restart", "--", service], check=True, timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.SubprocessError, OSError):
        raise SyncError("CLIProxyAPI restart failed; inspect the service journal privately.") from None


def verify_models(base_url, key, expected):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            actual = model_ids(base_url, key)
            if expected.issubset(actual):
                return len(actual)
        except SyncError:
            pass
        time.sleep(1)
    raise SyncError("Model verification timed out; expected models are missing or the API is unavailable.")


def apply_update(path, original, updated, service, base_url, key, expected):
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise SyncError("Use a regular configuration file readable only by its owner (mode 0600).")
    fd, filename = tempfile.mkstemp(prefix=path.name + ".bak-", dir=path.parent)
    backup = Path(filename)
    with os.fdopen(fd, "wb") as stream:
        stream.write(original)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        replace_config(path, updated, metadata, original)
    except SyncError as error:
        raise SyncError(f"{error} Rerun the preview. Backup: {backup}") from None
    try:
        restart(service)
        total = verify_models(base_url, key, expected)
        if path.read_bytes() != updated:
            raise SyncError("Configuration changed during verification; inspect concurrent edits.")
    except SyncError as error:
        if path.read_bytes() != updated:
            raise SyncError(f"Concurrent edit left in place. Original backup: {backup}") from None
        try:
            replace_config(path, original, metadata, updated)
            restart(service)
        except SyncError:
            raise SyncError(f"Recovery could not complete; inspect the current configuration and backup {backup}.") from None
        raise SyncError(f"{error} Original configuration restored and service restarted. Backup: {backup}") from None
    return backup, total


def arguments():
    parser = argparse.ArgumentParser(description=__doc__, epilog="Requires PyYAML. Default: preview only; --apply backs up, writes, restarts, and verifies.")
    parser.add_argument("--config", type=Path, default=Path("/var/lib/cli-proxy-api/config.yaml"))
    parser.add_argument("--provider", default="magpie", help="existing openai-compatibility provider name")
    parser.add_argument("--prefix", action="append", default=[], help="include model IDs with this literal prefix; repeatable; default: all")
    parser.add_argument("--api-base-url", help="CLIProxyAPI client base URL; default: derive from its configuration")
    parser.add_argument("--service", default="cli-proxy-api.service", help="systemd service restarted by --apply")
    parser.add_argument("--apply", action="store_true", help="apply additions and restart CLIProxyAPI; requires root")
    parser.add_argument("--json", action="store_true", help="print a JSON result instead of human-readable output")
    return parser.parse_args()


def main():
    args = arguments()
    if args.apply and os.geteuid() != 0:
        raise SyncError("Run --apply as root on the CLIProxyAPI host.")
    original = args.config.read_bytes()
    text = original.decode("utf-8")
    data, provider, _ = parse_config(text, args.provider)
    credentials = provider.get("keys", provider.get("api-key-entries", []))
    upstream_key = credentials[0].get("api-key") if credentials else None
    source = model_ids(provider.get("base-url"), upstream_key)
    selected = {name for name in source if not args.prefix or name.startswith(tuple(args.prefix))}
    updated, additions = add_mappings(text, args.provider, selected)
    result = {"applied": False, "added": additions, "already_configured": len(selected) - len(additions), "backup": None, "total": None}
    if args.apply and additions:
        base_url, key = client_connection(data, args.api_base_url)
        baseline = model_ids(base_url, key)
        backup, total = apply_update(args.config, original, updated.encode("utf-8"), args.service, base_url, key, baseline | set(additions))
        result.update(applied=True, backup=str(backup), total=total)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        action = "Added" if result["applied"] else "Would add" if additions else "No changes:"
        print(f"{action} {len(additions)} models; {result['already_configured']} already configured.")
        for name in additions:
            print(f"  {name}")
        if result["backup"]:
            print(f"Backup: {result['backup']}\nVerified {result['total']} published models after restart.")


if __name__ == "__main__":
    try:
        main()
    except (SyncError, OSError, UnicodeError) as error:
        message = str(error) if isinstance(error, SyncError) else "Cannot read or write configuration; check the path, permissions, and UTF-8 encoding."
        print(f"ERROR: {message}", file=sys.stderr)
        sys.exit(1)
