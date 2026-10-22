# Magpie server helper

> English | [简体中文](README.zh-CN.md)

Install [Magpie](https://github.com/yetone/magpie) on an Ubuntu server with a public IPv4 address, HTTPS, a dedicated service account, and a systemd service. Manage it through an SSH tunnel by default. The install and uninstall scripts follow the layout of `cliproxyapi-helper`.

## Requirements

- Ubuntu with systemd, root access, and an `amd64` or `arm64` CPU.
- A public IPv4 assigned directly to the host. NAT-only hosts and domain certificates are outside this installer's scope.
- Ports `80` and `443` free. Public TCP port `80` must remain reachable for certificate issuance and renewal; TCP `443` serves the model API. [`--local-only`](#use-as-a-cliproxyapi-upstream) needs no public address or certificate ports.
- Access to GitHub, Ubuntu package repositories, Snap Store, and Let's Encrypt; `--local-only` needs only GitHub and Ubuntu package repositories.

The management page uses a separate port, `3430`, bound to `127.0.0.1` by default. The scripts do not change firewalls or cloud security groups.

## Install

Replace the documentation address `203.0.113.10` with the host's real public IP. From the machine holding these scripts:

```bash
ssh root@203.0.113.10 'bash -s -- --ip 203.0.113.10' < ./install-magpie.sh
```

The installer downloads the terminal-only Magpie release, verifies the asset's SHA256 against the official GitHub release metadata, creates the `magpie` system user, requests an IP HTTPS certificate with Certbot, and starts `magpie.service`. The default version is pinned to `v0.1.1268`.

The final checks verify HTTPS, rejection of unauthenticated model API and management requests, successful management authentication, initial client-key authentication on a fresh install, and a Certbot renewal dry run. With `--local-only`, they instead verify that a forwarded loopback request reaches the model API and that management rejects unauthenticated requests and accepts the management key. A failed installation exits nonzero and leaves its diagnostics visible; it does not automatically undo partial changes.

### Manage through SSH

On your computer, keep this connection open:

```bash
ssh -N -L 3430:127.0.0.1:3430 root@203.0.113.10
```

Open `http://127.0.0.1:3430`. Retrieve the management key **privately** from `/etc/magpie/service.env`, specifically `MAGPIE_WEB_KEY`, and paste it into the page's key field. For example, open that file using `sudoedit` in an interactive SSH session. Do not copy the file or key into logs, issues, chat, or version control. The page grants access to provider credentials and settings.

Add providers and accounts in the browser. For a subscription sign-in that redirects to localhost on your computer, copy the browser's final callback URL into Magpie's **Callback URL** field.

You can also use the Magpie CLI on the server. Run it as the service user with the service's HOME and XDG directories, so it writes the same settings the service reads. For example, a GitHub Copilot subscription uses a device code printed in the terminal:

```bash
sudo runuser -u magpie -- env $(sudo grep -E '^(HOME|XDG_[A-Z_]+)=' /etc/magpie/service.env) \
  /usr/local/bin/magpie accounts add copilot
sudo systemctl restart magpie
```

Use `magpie provider add <preset> <key>` the same way for API-key providers. The running service does not load providers or accounts added by the CLI until it restarts.

### Use as a CLIProxyAPI upstream

When `cliproxyapi-helper` already serves TCP `443` on the same host, install Magpie with `--local-only` so clients keep using CLIProxyAPI and Magpie stays private: `client → CLIProxyAPI (https://<ip>/v1) → Magpie (http://127.0.0.1:3425/v1)`.

```bash
ssh root@203.0.113.10 'bash -s -- --local-only' < ./install-magpie.sh
```

This mode needs neither `--ip` nor ports `80`/`443`, requests no certificate, and installs no Certbot. The model API listens only on `127.0.0.1:3425` over plain HTTP; management stays on `127.0.0.1:3430` via the SSH tunnel above. It sets `MAGPIE_TRUST_PROXY=1`, so requests CLIProxyAPI forwards over loopback are served as local ones even when they carry a forwarding header; CLIProxyAPI authenticates its own clients. Any local process can call this loopback API, as with any default Magpie install. `--local-only` cannot be combined with `--ip` or `--enable-remote-management`; running it over a public install returns management to loopback, while the old certificate lineage and its renewal remain until removed with Certbot.

The installer does not edit CLIProxyAPI. After adding providers in Magpie, use the optional [model sync script](#sync-models-to-cliproxyapi), or list model names on the server with `curl -s http://127.0.0.1:3425/v1/models` and add them in the CLIProxyAPI management page or `config.yaml`. With `config-version: 8`, the group belongs under `api-keys`:

```yaml
api-keys:
  openai-compatibility:
    - name: "magpie"
      base-url: "http://127.0.0.1:3425/v1"
      keys:
        - api-key: "<contents of /etc/magpie/client.key>"
      models:
        - name: "copilot/gpt-5.5"           # model name from Magpie
          alias: "copilot/gpt-5.5"          # name clients will request
```

Older configs without `config-version: 8` put `openai-compatibility` at the top level. To namespace models, write the full name in each `alias` rather than setting `prefix`: CLIProxyAPI still publishes the unprefixed aliases alongside the prefixed ones, and those can collide with models from other upstreams. After editing, check CLIProxyAPI's `/v1/models`; automatic reload may not detect an atomic file replacement. The sync script explicitly restarts CLIProxyAPI when applying additions.

Magpie accepts any token on loopback, but the initial gateway key attributes usage to a named identity. Clients keep CLIProxyAPI's base URL and API key.

### Sync models to CLIProxyAPI

`sync-magpie-models.py` reads the existing Magpie provider's `base-url`, fetches its live model list, and adds missing mappings. It supports version 8 and legacy configuration layouts. Existing aliases, mappings for models no longer listed upstream, comments, and unrelated configuration remain unchanged. It does not fix stale model names or create a provider: first configure the `magpie` entry above, with `models: []` if empty and without a provider-level `prefix`. Shared YAML model references must be expanded before editing.

Install the sync script's only extra dependency **on the target host**:

```bash
ssh root@203.0.113.10 'apt-get update && apt-get install -y python3-yaml'
```

Preview selected model groups, without writing files or restarting services:

```bash
ssh root@203.0.113.10 'python3 - --prefix qoder/ --prefix opencode-go/' < ./sync-magpie-models.py
```

Apply the same additions:

```bash
ssh root@203.0.113.10 'python3 - --prefix qoder/ --prefix opencode-go/ --apply' < ./sync-magpie-models.py
```

Omit `--prefix` to include all upstream models. Reruns add only missing upstream names and keep existing aliases; when there are no additions, they create no backup and do not restart the service. `--apply` requires root, saves an owner-only `config.yaml.bak-*` beside the configuration, preserves its owner and permissions, restarts CLIProxyAPI, and checks that the previous model names plus the additions are published. Restarting briefly interrupts requests. On restart or verification failure it restores the previous configuration and restarts again; a detected intervening external edit is left intact and reported with the backup path. Run the command while no other tool or person is editing this configuration. The checks run immediately before each file replacement and after verification; they do not provide atomic coordination with arbitrary external writers. Model-list verification does not send inference requests. Keys stay inside the server process and are never printed. HTTPS certificates are verified, and authenticated requests do not follow redirects.

| Option | Default | Purpose |
|---|---|---|
| `--config` | `/var/lib/cli-proxy-api/config.yaml` | Existing private CLIProxyAPI YAML configuration |
| `--provider` | `magpie` | Existing `openai-compatibility` entry to extend |
| `--prefix` | All models | Include a literal model ID prefix; repeat to select multiple groups |
| `--api-base-url` | Derived from the configuration | CLIProxyAPI client URL, such as `https://203.0.113.10/v1`; required if the TLS listener address does not match its certificate |
| `--service` | `cli-proxy-api.service` | systemd service restarted by `--apply` |
| `--apply` | Off: preview only | Back up, write additions, restart, and verify |
| `--json` | Off | Print a JSON result; errors remain on stderr |

For a version 8 config, the client key comes from `access.api-keys`; legacy configs use the top-level `api-keys` list. No key needs to appear in the command. Exit codes are `0` for a successful preview, no-op, or verified update, `1` for an operation failure, and `2` for invalid arguments. Inspect the printed backup and restore it privately if manual recovery is needed; do not copy its contents to logs or version control.

### Connect clients

Configure a remote client with the model API base URL `https://203.0.113.10/v1` and an enabled gateway key. A fresh install creates one initial key in `/etc/magpie/client.key`, readable only by root. Retrieve it privately as above. Further keys, restrictions, and revocation are managed under **Gateway → Gateway keys**.

Magpie supports OpenAI Chat, OpenAI Responses, and Anthropic Messages; use the client's required endpoint under that base URL. A server does not discover or configure agent installations on other computers. Model calls require a configured upstream provider.

## Options

| Option | Default | Meaning |
|---|---|---|
| `--ip <public-ipv4>` | Required unless `--local-only` | Certificate identity and API bind address |
| `--local-only` | Off | Serve the API on `127.0.0.1` over HTTP without a certificate, as a local proxy's upstream |
| `--gateway-port <port>` | `443` (`3425` with `--local-only`) | Model API port |
| `--management-port <port>` | `3430` | Separate browser page port |
| `--enable-remote-management` | Off on fresh installs | Expose management on the public IP over HTTPS |
| `--disable-remote-management` | — | Return management to loopback for SSH access |
| `--magpie-version <tag\|latest>` | `v0.1.1268` | Install a release tag or resolve the latest stable release |
| `--acme-email <email>` | Unset | Let's Encrypt account email |
| `-h`, `--help` | — | Print help without modifying the host |

Command options override `MAGPIE_VERSION` and `ACME_EMAIL`, which override the built-in defaults. Ports must differ, and neither can be `80`.

Remote management is an explicit choice, not needed for SSH access:

```bash
ssh root@203.0.113.10 \
  'bash -s -- --ip 203.0.113.10 --enable-remote-management' < ./install-magpie.sh
```

This exposes `https://203.0.113.10:3430`; anyone holding its management key can change settings and read provider keys. Open the chosen port in your own network policy only if you intend to use it.

## Reinstall, upgrade, and recover

Run the same command with `--magpie-version <tag>` to upgrade or reinstall. Without a management switch it preserves the current management binding and key. It keeps all provider settings, accounts, usage data, and gateway keys. It does not restore a revoked initial client key or adopt another installation. After revoking the initial key, `/etc/magpie/client.key` is a stale copy: use another key from the management page.

The binary and environment are root-owned. Use this installer for upgrades rather than the browser's updater. Back up `/etc/magpie` and `/var/lib/magpie` privately before upgrading; older Magpie versions may not understand newer data. To recover an interrupted install, inspect the service logs and rerun the installer after resolving the reported error. No automatic downgrade or state rollback is provided.

```bash
ssh root@203.0.113.10 'systemctl status magpie.service --no-pager'
ssh root@203.0.113.10 'journalctl -u magpie.service -n 100 --no-pager'
```

The unit discards standard output because Magpie's startup banner includes a management key. Errors sent to standard error remain in the journal. Certificate renewal copies the new certificate into the service's TLS directory; Magpie reads changed certificates without a restart.

## Files

| Path | Purpose |
|---|---|
| `/usr/local/bin/magpie` | Root-owned terminal binary |
| `/etc/systemd/system/magpie.service` | System service running as `magpie` |
| `/etc/magpie/service.env` | Root-only management key and managed environment |
| `/etc/magpie/client.key` | Root-only copy of the initial gateway key, if created |
| `/etc/magpie/tls/` | Certificate copies readable by the service (unused with `--local-only`) |
| `/var/lib/magpie/` | Private HOME, provider accounts, settings, cache, data, and state |
| `/etc/letsencrypt/live/magpie-<ip>/` | Certbot-owned certificate lineage (not created with `--local-only`) |
| `/etc/letsencrypt/renewal-hooks/deploy/50-magpie` | Copy renewed certificates into the service directory (not created with `--local-only`) |

## Uninstall

Preview an uninstall, including a full deletion if requested:

```bash
ssh root@203.0.113.10 'bash -s -- --purge --dry-run' < ./uninstall-magpie.sh
```

Remove the service, binary, and renewal hook while keeping all accounts and data:

```bash
ssh root@203.0.113.10 'bash -s -- --yes' < ./uninstall-magpie.sh
```

**Destructive:** add `--purge` to permanently delete `/etc/magpie`, `/var/lib/magpie`, and the dedicated service user/group. Back up anything needed first. Without `--yes`, an interactive terminal asks you to type `uninstall`; non-interactive execution requires `--yes`.

All modes retain Certbot, its renewal timer, and every certificate under `/etc/letsencrypt`, including `magpie-<ip>`. Inspect certificate usage before deleting a lineage separately with Certbot. The helper does not own that shared certificate store.

## Validation

Create a local test environment with the sync dependency:

```bash
python3 -m venv /tmp/magpie-helper-venv
/tmp/magpie-helper-venv/bin/python -m pip install -r requirements.txt
```

```bash
bash -n install-magpie.sh uninstall-magpie.sh
shellcheck install-magpie.sh uninstall-magpie.sh
/tmp/magpie-helper-venv/bin/python -m unittest discover -s tests -v
```

Tests use temporary directories and replace host operations. They do not install packages, change a real service, or request a public certificate. Production Ubuntu installation and Let's Encrypt issuance require a real target host and remain separate checks.

References: [Magpie deployment reference](https://github.com/yetone/magpie/blob/main/docs/reference.md#docker), [official releases](https://github.com/yetone/magpie-releases/releases), [Let's Encrypt IP certificates and Certbot](https://letsencrypt.org/2026/03/11/shorter-certs-certbot/).

## License

[MIT](LICENSE)
