#!/usr/bin/env bash
set +x
set -Eeuo pipefail
umask 077

service_name=magpie.service
service_user=magpie
config_dir=/etc/magpie
state_dir=/var/lib/magpie
binary_path=/usr/local/bin/magpie
service_path=/etc/systemd/system/magpie.service
hook_path=/etc/letsencrypt/renewal-hooks/deploy/50-magpie
public_ip=''
local_only=false
gateway_port=''
management_port=3430
management_mode=preserve
requested_version="${MAGPIE_VERSION:-v0.1.1268}"
acme_email="${ACME_EMAIL:-}"
download_dir=''
created_client_key=false

log() { printf '[magpie] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }
usage_error() { log "ERROR: $*"; usage >&2; exit 2; }

usage() {
  cat <<'EOF'
Install Magpie on Ubuntu with systemd and an IP HTTPS certificate.

Usage: install-magpie.sh --ip <public-ipv4> [options]
       install-magpie.sh --local-only [options]

  --ip <public-ipv4>          Public IPv4 assigned to this host (required
                             unless --local-only)
  --local-only               Serve plain HTTP on 127.0.0.1 only, without a
                             certificate, as the upstream of a local proxy
                             such as CLIProxyAPI (trusts its forwarding)
  --gateway-port <port>      Model API port (default: 443; 3425 with --local-only)
  --management-port <port>   Separate browser management port (default: 3430)
  --enable-remote-management  Bind management to the public IP over HTTPS
  --disable-remote-management Bind management to loopback for SSH access
                             New installs use loopback; reinstalls preserve it.
  --magpie-version <tag|latest> Release (default: v0.1.1268; MAGPIE_VERSION)
  --acme-email <email>       Let's Encrypt account email (or ACME_EMAIL)
  -h, --help                Show help without changing the host

Example: install-magpie.sh --ip 203.0.113.10
         install-magpie.sh --local-only
The installer never prints keys. Without --local-only, port 80 must be free
and publicly reachable.
EOF
}

require_value() {
  [[ -n "${2:-}" && "$2" != -* ]] || usage_error "$1 requires a value."
}

parse_args() {
  while (($#)); do
    case "$1" in
      --ip) require_value "$1" "${2:-}"; public_ip="$2"; shift 2 ;;
      --local-only) local_only=true; shift ;;
      --gateway-port) require_value "$1" "${2:-}"; gateway_port="$2"; shift 2 ;;
      --management-port) require_value "$1" "${2:-}"; management_port="$2"; shift 2 ;;
      --magpie-version) require_value "$1" "${2:-}"; requested_version="$2"; shift 2 ;;
      --acme-email) require_value "$1" "${2:-}"; acme_email="$2"; shift 2 ;;
      --enable-remote-management|--disable-remote-management)
        local mode=enable
        [[ "$1" != --disable-remote-management ]] || mode=disable
        [[ "$management_mode" == preserve || "$management_mode" == "$mode" ]] \
          || usage_error "Remote management flags are mutually exclusive."
        management_mode="$mode"; shift ;;
      -h|--help) usage; exit 0 ;;
      *) usage_error "Unknown option: $1" ;;
    esac
  done
  validate_args
}

validate_args() {
  if [[ "$local_only" == true ]]; then
    [[ -z "$public_ip" ]] || usage_error "--ip cannot be combined with --local-only."
    [[ "$management_mode" != enable ]] \
      || usage_error "--enable-remote-management cannot be combined with --local-only."
    : "${gateway_port:=3425}"
  else
    [[ -n "$public_ip" ]] || usage_error "--ip is required unless --local-only is used."
    : "${gateway_port:=443}"
  fi
  [[ "$requested_version" == latest || "$requested_version" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] \
    || usage_error "--magpie-version must be vX.Y.Z or latest."
  local port
  for port in "$gateway_port" "$management_port"; do
    [[ "$port" =~ ^[1-9][0-9]{0,4}$ ]] || usage_error "Ports must be integers from 1 to 65535."
    ((port <= 65535 && port != 80)) || usage_error "Port 80 is reserved for certificate renewal; use another port from 1 to 65535."
  done
  [[ "$gateway_port" != "$management_port" ]] || usage_error "The model API and management page need different ports."
  [[ "$local_only" == true || "$public_ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || usage_error "--ip must be an IPv4 address."
}

validate_host() {
  [[ $EUID -eq 0 ]] || die "Run as root on the target Ubuntu host."
  [[ "$(ps -p 1 -o comm= | tr -d ' ')" == systemd ]] || die "A running systemd host is required."
  [[ "$(sed -n 's/^ID=//p' /etc/os-release | tr -d '\"')" == ubuntu ]] || die "Only Ubuntu is supported."
  case "$(uname -m)" in x86_64|aarch64|arm64) ;; *) die "Only amd64 and arm64 are supported." ;; esac
  if [[ -e "$service_path" || -e "${config_dir}/service.env" ]]; then
    local file
    for file in "$service_path" "${config_dir}/service.env"; do
      [[ ! -e "$file" ]] || grep -Fqx '# Managed by install-magpie.sh.' "$file" \
        || die "Unmanaged installation at $file; leave it in place and choose a separate host."
    done
  elif [[ -e "$binary_path" || -e "$config_dir" || -e "$state_dir" || -e "$hook_path" ]]; then
    die "Existing Magpie files are not owned by this installer."
  fi
}

install_dependencies() {
  log 'Installing dependencies'
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates curl iproute2 openssl python3 util-linux
  [[ "$local_only" == false ]] || return 0
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends snapd
  systemctl enable --now snapd.socket
  timeout 180 snap wait system seed.loaded
  if snap list certbot >/dev/null 2>&1; then snap refresh certbot; else snap install certbot --classic; fi
  local version
  version="$(/snap/bin/certbot --version)"
  dpkg --compare-versions "${version##* }" ge 5.4.0 || die 'Certbot 5.4 or newer is required.'
  systemctl enable --now snap.certbot.renew.timer
}

validate_public_ip() {
  python3 - "$public_ip" <<'PY'
import ipaddress, sys
try:
    address = ipaddress.IPv4Address(sys.argv[1])
except ValueError:
    raise SystemExit("Invalid IPv4 address")
if not address.is_global:
    raise SystemExit("--ip must be a public IPv4 address")
PY
  ip -4 -o address show scope global | awk '{sub(/\/.*/, "", $4); print $4}' \
    | grep -Fqx -- "$public_ip" || die "$public_ip is not assigned to this host."
}

validate_network() {
  local ports=("$gateway_port" "$management_port")
  if [[ "$local_only" == false ]]; then
    validate_public_ip
    ports=(80 "${ports[@]}")
  fi
  local port listener listeners pid
  pid="$(systemctl show "$service_name" --property MainPID --value)"
  for port in "${ports[@]}"; do
    listeners="$(ss -H -ltnp "sport = :${port}")"
    while IFS= read -r listener; do
      [[ -z "$listener" ]] && continue
      [[ "$port" != 80 && "$pid" =~ ^[1-9][0-9]*$ && "$listener" == *"pid=${pid},"* ]] \
        || die "Port $port is occupied; inspect it with ss -lntp before continuing."
    done <<<"$listeners"
  done
}

download_release() {
  local arch metadata asset_url expected actual endpoint
  case "$(uname -m)" in x86_64) arch=amd64 ;; *) arch=arm64 ;; esac
  endpoint="tags/${requested_version}"
  [[ "$requested_version" != latest ]] || endpoint=latest
  download_dir="$(mktemp -d /tmp/magpie-install.XXXXXX)"
  curl -fsSL --connect-timeout 15 --max-time 60 -o "${download_dir}/release.json" \
    "https://api.github.com/repos/yetone/magpie-releases/releases/${endpoint}"
  metadata="$(python3 - "${download_dir}/release.json" "$arch" "$requested_version" <<'PY'
import json, re, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    release = json.load(handle)
tag = release["tag_name"]
if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag) or release["draft"] or release["prerelease"]:
    raise SystemExit("Expected a published stable release")
if sys.argv[3] != "latest" and tag != sys.argv[3]:
    raise SystemExit("Release tag does not match the requested version")
name = "magpie-cli-linux-" + sys.argv[2]
asset = next(a for a in release["assets"] if a["name"] == name)
url = f"https://github.com/yetone/magpie-releases/releases/download/{tag}/{name}"
digest = asset.get("digest") or ""
if asset["browser_download_url"] != url or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
    raise SystemExit("Release asset needs its official URL and SHA256 digest")
print(tag, url, digest.removeprefix("sha256:"))
PY
)"
  read -r release_tag asset_url expected <<<"$metadata"
  log "Downloading Magpie ${release_tag} (${arch})"
  curl -fsSL --connect-timeout 15 --max-time 300 -o "${download_dir}/magpie" "$asset_url"
  actual="$(sha256sum "${download_dir}/magpie")"
  [[ "${actual%% *}" == "$expected" ]] || die 'Release checksum mismatch; nothing was installed.'
}

prepare_runtime() {
  if id -u "$service_user" >/dev/null 2>&1; then
    [[ "$(getent passwd "$service_user" | cut -d: -f6-7)" == "${state_dir}:/usr/sbin/nologin" ]] \
      || die 'The existing magpie user is not the dedicated service account.'
  else
    useradd --system --user-group --home-dir "$state_dir" --no-create-home --shell /usr/sbin/nologin "$service_user"
  fi
  install -d -o root -g root -m 0700 "$config_dir"
  install -d -o "$service_user" -g "$service_user" -m 0700 "$state_dir"
  install -d -o root -g "$service_user" -m 0750 "${config_dir}/tls"
  local key mode=false web_host=127.0.0.1
  if [[ -f "${config_dir}/service.env" ]]; then
    key="$(sed -n 's/^MAGPIE_WEB_KEY=//p' "${config_dir}/service.env")"
    mode="$(sed -n 's/^MAGPIE_HELPER_REMOTE_MANAGEMENT=//p' "${config_dir}/service.env")"
    [[ "$key" =~ ^[a-zA-Z0-9._~-]{16,}$ && "$mode" =~ ^(true|false)$ ]] \
      || die 'Cannot read the managed environment; restore it before reinstalling.'
  else
    key="$(openssl rand -hex 32)"
  fi
  case "$management_mode" in enable) mode=true ;; disable) mode=false ;; esac
  [[ "$local_only" == false ]] || mode=false
  [[ "$mode" != true ]] || web_host="$public_ip"
  local network_env
  if [[ "$local_only" == true ]]; then
    # CLIProxyAPI authenticates its own clients; keep its loopback requests local.
    network_env="MAGPIE_ADDR=127.0.0.1:${gateway_port}
MAGPIE_TRUST_PROXY=1"
  else
    network_env="MAGPIE_ADDR=${public_ip}:${gateway_port}
MAGPIE_PUBLIC_URL=https://${public_ip}:${gateway_port}"
  fi
  cat >"${config_dir}/service.env" <<EOF
# Managed by install-magpie.sh.
HOME=${state_dir}
XDG_CONFIG_HOME=${state_dir}/config
XDG_CACHE_HOME=${state_dir}/cache
XDG_DATA_HOME=${state_dir}/data
XDG_STATE_HOME=${state_dir}/state
${network_env}
MAGPIE_WEB_KEY=${key}
MAGPIE_HELPER_REMOTE_MANAGEMENT=${mode}
EOF
  if [[ "$local_only" == false ]]; then
    printf 'MAGPIE_TLS_CERT=%s\nMAGPIE_TLS_KEY=%s\n' \
      "${config_dir}/tls/fullchain.pem" "${config_dir}/tls/privkey.pem" >>"${config_dir}/service.env"
  fi
  chmod 0600 "${config_dir}/service.env"
  web_addr="${web_host}:${management_port}"
}

provision_certificate() {
  local args=(certonly --non-interactive --agree-tos --standalone --preferred-challenges http-01
    --preferred-profile shortlived --ip-address "$public_ip" --cert-name "magpie-${public_ip}" --keep-until-expiring)
  if [[ -n "$acme_email" ]]; then args+=(--email "$acme_email"); else args+=(--register-unsafely-without-email); fi
  install -d -o root -g root -m 0755 "$(dirname "$hook_path")"
  cat >"$hook_path" <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail
[[ "\${RENEWED_LINEAGE:-}" == /etc/letsencrypt/live/magpie-${public_ip} ]] || exit 0
install -o root -g ${service_user} -m 0640 "\${RENEWED_LINEAGE}/fullchain.pem" ${config_dir}/tls/fullchain.pem
install -o root -g ${service_user} -m 0640 "\${RENEWED_LINEAGE}/privkey.pem" ${config_dir}/tls/privkey.pem
EOF
  chmod 0750 "$hook_path"
  /snap/bin/certbot "${args[@]}"
  RENEWED_LINEAGE="/etc/letsencrypt/live/magpie-${public_ip}" bash "$hook_path"
  openssl x509 -in "${config_dir}/tls/fullchain.pem" -noout -checkip "$public_ip" >/dev/null
}

install_service() {
  # The service may read its certificate, but only root can read its environment.
  chmod 0711 "$config_dir"
  install -d -m 0755 "$(dirname "$binary_path")"
  install -o root -g root -m 0755 "${download_dir}/magpie" "${binary_path}.new"
  mv -f "${binary_path}.new" "$binary_path"
  if [[ ! -s "${config_dir}/client.key" && ! -e "${state_dir}/config/magpie/caller-keys.json" ]]; then
    runuser -u "$service_user" -- env HOME="$state_dir" XDG_CONFIG_HOME="${state_dir}/config" \
      XDG_CACHE_HOME="${state_dir}/cache" XDG_DATA_HOME="${state_dir}/data" XDG_STATE_HOME="${state_dir}/state" \
      "$binary_path" gateway-key add 'Initial server client' >"${config_dir}/client.key"
    [[ -s "${config_dir}/client.key" ]] || die 'Magpie did not create the initial gateway key.'
    created_client_key=true
  fi
  cat >"$service_path" <<EOF
# Managed by install-magpie.sh.
[Unit]
Description=Magpie model gateway and browser management
Wants=network-online.target
After=network-online.target

[Service]
User=${service_user}
Group=${service_user}
WorkingDirectory=${state_dir}
EnvironmentFile=${config_dir}/service.env
ExecStart=${binary_path} serve --web=${web_addr}
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=${state_dir}
StandardOutput=null
StandardError=journal
EOF
  if ((gateway_port < 1024 || management_port < 1024)); then
    printf 'AmbientCapabilities=CAP_NET_BIND_SERVICE\nCapabilityBoundingSet=CAP_NET_BIND_SERVICE\n' >>"$service_path"
  else
    printf 'CapabilityBoundingSet=\n' >>"$service_path"
  fi
  printf '\n[Install]\nWantedBy=multi-user.target\n' >>"$service_path"
  chmod 0644 "$service_path"
  systemctl daemon-reload
  systemctl enable "$service_name"
  systemctl restart "$service_name"
}

verify_installation() {
  systemctl is-enabled --quiet "$service_name"
  local attempt api_base="https://${public_ip}:${gateway_port}"
  [[ "$local_only" == false ]] || api_base="http://127.0.0.1:${gateway_port}"
  for ((attempt=0; attempt<15; attempt++)); do
    if curl --noproxy '*' -sS --connect-timeout 1 --max-time 2 \
      "${api_base}/v1/models" >/dev/null 2>&1; then break; fi
    sleep 1
  done
  python3 - "$config_dir" "$api_base" "$web_addr" "$created_client_key" "$local_only" <<'PY'
from pathlib import Path
import sys, urllib.error, urllib.request
root, api_base, web, created_key, local_only = sys.argv[1:]
env = dict(line.split("=", 1) for line in Path(root, "service.env").read_text().splitlines() if "=" in line)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def check(url, expected, headers):
    try:
        with opener.open(urllib.request.Request(url, headers=headers), timeout=10) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    if status != expected:
        raise SystemExit(f"Installation check failed: expected HTTP {expected}, got {status}")
url = api_base + "/v1/models"
if local_only == "true":
    # A trusted local proxy's forwarded request must be served as a local one.
    check(url, 200, {"X-Forwarded-For": "198.51.100.254", "Authorization": "Bearer local-proxy-check"})
else:
    headers = {"X-Forwarded-For": "198.51.100.254"}
    check(url, 401, headers)
    # A previously revoked initial key is not re-enabled by reinstalling.
    if created_key == "true":
        key = Path(root, "client.key").read_text().strip()
        headers["Authorization"] = "Bearer " + key
        check(url, 200, headers)
scheme = "http" if web.startswith("127.0.0.1:") else "https"
url = f"{scheme}://{web}/api/state"
check(url, 401, {})
check(url, 200, {"Cookie": f"magpie_web_{web.rsplit(':', 1)[1]}=" + env["MAGPIE_WEB_KEY"]})
print("Verified gateway access, management authentication, and transport.")
PY
  [[ "$local_only" == true ]] || /snap/bin/certbot renew --cert-name "magpie-${public_ip}" --dry-run
}

cleanup() {
  if [[ -n "$download_dir" ]]; then
    find "$download_dir" -depth -delete
  fi
}

main() {
  parse_args "$@"
  validate_host
  install_dependencies
  validate_network
  download_release
  prepare_runtime
  [[ "$local_only" == true ]] || provision_certificate
  install_service
  verify_installation
  if [[ "$local_only" == true ]]; then
    log "Installed ${release_tag}; local API http://127.0.0.1:${gateway_port}/v1 (not public)"
  else
    log "Installed ${release_tag}; API https://${public_ip}:${gateway_port}/v1"
  fi
  log "Management ${web_addr}; keys are in ${config_dir}/service.env and ${config_dir}/client.key (if created)."
  log 'See README for SSH access, provider sign-in, and retrieving keys privately.'
}

if [[ "${BASH_SOURCE[0]:-$0}" == "$0" ]]; then
  trap cleanup EXIT
  main "$@"
fi
