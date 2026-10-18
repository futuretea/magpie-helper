#!/usr/bin/env bash
set +x
set -Eeuo pipefail

service_name=magpie.service
service_user=magpie
config_dir=/etc/magpie
state_dir=/var/lib/magpie
binary_path=/usr/local/bin/magpie
service_path=/etc/systemd/system/magpie.service
hook_path=/etc/letsencrypt/renewal-hooks/deploy/50-magpie
purge=false
dry_run=false
assume_yes=false

die() { printf '[magpie-uninstall] ERROR: %s\n' "$*" >&2; exit 1; }
usage() {
  cat <<'EOF'
Uninstall the Magpie installation owned by install-magpie.sh.

Usage: uninstall-magpie.sh [--purge] [--dry-run] [--yes]

  --purge    Also delete /etc/magpie, /var/lib/magpie, and the service user
             This permanently deletes provider accounts, keys, and usage data.
  --dry-run  List the targets without changing the host
  --yes      Confirm a non-interactive uninstall
  -h, --help Show help

Default: keep accounts, keys, data, certificates, and the service user.
Certbot, its renewal timer, and all Let's Encrypt certificates are retained.
EOF
}

parse_args() {
  while (($#)); do
    case "$1" in
      --purge) purge=true ;;
      --dry-run) dry_run=true ;;
      --yes) assume_yes=true ;;
      -h|--help) usage; exit 0 ;;
      *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
  done
}

validate_host() {
  [[ $EUID -eq 0 ]] || die 'Run as root on the target host.'
  [[ "$(ps -p 1 -o comm= | tr -d ' ')" == systemd ]] || die 'A running systemd host is required.'
  check_ownership
}

check_ownership() {
  local file found=false
  for file in "$service_path" "${config_dir}/service.env"; do
    if [[ -e "$file" ]]; then
      grep -Fqx '# Managed by install-magpie.sh.' "$file" || die "Unmanaged installation at $file."
      found=true
    fi
  done
  [[ "$found" == true ]] || die 'No installation owned by install-magpie.sh was found.'
}

print_plan() {
  printf 'Remove service: %s\nRemove binary: %s\n' "$service_path" "$binary_path"
  [[ ! -e "$hook_path" ]] || printf 'Remove renewal hook: %s\n' "$hook_path"
  if [[ "$purge" == true ]]; then
    printf 'Permanently delete: %s, %s, user/group %s\n' "$config_dir" "$state_dir" "$service_user"
  else
    printf 'Preserve: %s, %s, and user/group %s\n' "$config_dir" "$state_dir" "$service_user"
  fi
  printf 'Preserve: Certbot, its renewal timer, and all /etc/letsencrypt certificates, if present.\n'
}

confirm_uninstall() {
  [[ "$dry_run" == false && "$assume_yes" == false ]] || return 0
  [[ -t 0 && -t 1 ]] || { printf 'Non-interactive uninstall requires --yes.\n' >&2; exit 2; }
  local answer
  printf 'Type "uninstall" to confirm the targets above: ' >&2
  read -r answer || answer=''
  [[ "$answer" == uninstall ]] || { printf 'Cancelled.\n' >&2; exit 3; }
}

purge_data() {
  local target
  if id -u "$service_user" >/dev/null 2>&1; then
    [[ "$(getent passwd "$service_user" | cut -d: -f6-7)" == "${state_dir}:/usr/sbin/nologin" ]] \
      || die 'The magpie user is not the dedicated service account.'
  fi
  for target in "$config_dir" "$state_dir"; do
    [[ ! -L "$target" ]] || die "Refusing to delete a symlink: $target"
    if [[ -d "$target" ]]; then
      find "$target" -xdev -depth -mindepth 1 -delete
      rmdir "$target"
    fi
  done
  if id -u "$service_user" >/dev/null 2>&1; then userdel "$service_user"; fi
  if getent group "$service_user" >/dev/null; then groupdel "$service_user"; fi
}

main() {
  parse_args "$@"
  validate_host
  print_plan
  confirm_uninstall
  if [[ "$dry_run" == true ]]; then printf 'Dry run complete; no changes made.\n'; return; fi
  if systemctl is-active --quiet "$service_name"; then systemctl stop "$service_name"; fi
  if systemctl is-enabled --quiet "$service_name"; then systemctl disable "$service_name"; fi
  local file
  for file in "$service_path" "$binary_path" "$hook_path"; do
    if [[ -e "$file" || -L "$file" ]]; then unlink "$file"; fi
  done
  systemctl daemon-reload
  [[ "$purge" == false ]] || purge_data
  printf 'Magpie uninstalled.\n'
}

if [[ "${BASH_SOURCE[0]:-$0}" == "$0" ]]; then main "$@"; fi
