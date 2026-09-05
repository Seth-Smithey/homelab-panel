#!/usr/bin/env bash
#
# homelab panel — installer for Ubuntu 24.04 (or any host with Python 3.11+)
#
#   sudo bash install.sh
#
# Layout — code is immutable per release, state lives beside it:
#
#   /opt/homelab-panel/
#     current -> releases/<commit>/     the running release (a symlink)
#     releases/<commit>/                code + its own venv, never edited
#     config.yaml  .env  data/          yours; never touched by a release
#     manifest.json                     what is deployed: version, commit, schema
#     panelctl                          the operator wrapper for this deployment
#   /var/backups/homelab-panel/         recovery points (install and update both make them)
#
# Activation is one transaction. Before anything running is touched, the new
# release is built in its own directory and validated against YOUR config, and
# a verified backup of the database, config, .env, manifest, wrapper and unit
# is taken. Then the symlink flips and the service restarts. If the new
# release does not become healthy, everything in that backup is put back —
# including the database, because a migrated database is something the
# previous release refuses to open — and the previous release is restarted.
#
# A release directory that exists is never written to again: the current
# release, the previous one and anything a backup points at are rollback
# targets and stay byte-for-byte as they were.

set -euo pipefail

APP_USER="panel"
APP_DIR="/opt/homelab-panel"
BACKUP_ROOT="/var/backups/homelab-panel"
SERVICE="homelab-panel"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEEP_RELEASES=5   # at least KEEP_BACKUPS in update.sh, or a backup may name a pruned release

if [[ $EUID -ne 0 ]]; then
  echo "Run this with sudo." >&2
  exit 1
fi

# PANEL_NO_SYSTEMD=1 runs everything except the unit management, and starts
# the release directly for the health check. It exists so CI can run THIS
# script — not a re-implementation of it — inside a container with no systemd.
NO_SYSTEMD="${PANEL_NO_SYSTEMD:-0}"

# shellcheck source=deploy/panel-lib.sh
source "$SRC_DIR/deploy/panel-lib.sh"

# Everything the panel writes is owner-only from the moment it is created.
umask 077
# Root running the release's python must not leave root-owned, mode-700
# __pycache__ directories inside release trees the service user reads.
export PYTHONDONTWRITEBYTECODE=1

take_lock

# ---------------------------------------------------------------------
# Identify this release
# ---------------------------------------------------------------------

# The version comes from the source tree being installed, read with plain
# tools so this works before any venv exists.
SRC_VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC_DIR/app/version.py" | head -1)"
[[ -n "$SRC_VERSION" ]] || die "$SRC_DIR/app/version.py has no __version__ — is this the right directory?"

# Root reading a clone owned by another user trips git's "dubious ownership"
# check and silently reports "not a git repo"; safe.directory scopes the
# exemption to exactly this path for exactly this invocation — never a
# global wildcard.
g() { git -c "safe.directory=$SRC_DIR" -C "$SRC_DIR" "$@"; }
if g rev-parse --git-dir >/dev/null 2>&1; then
  SRC_COMMIT="$(g rev-parse --short=12 HEAD 2>/dev/null || true)"
  SRC_DIRTY="$(g status --porcelain 2>/dev/null | head -1)"
else
  SRC_COMMIT=""
  SRC_DIRTY=""
fi
# A release id that is stable for a given commit, and unique for a dirty tree
# or a non-git copy so two different installs never share a directory.
if [[ -n "$SRC_COMMIT" && -z "$SRC_DIRTY" ]]; then
  RELEASE_ID="$SRC_COMMIT"
else
  RELEASE_ID="${SRC_VERSION}-$(date +%Y%m%d%H%M%S)"
  [[ -n "$SRC_DIRTY" ]] && warn "installing from a checkout with local changes"
fi
RELEASE_DIR="$APP_DIR/releases/$RELEASE_ID"

# ---------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------

say "Preflight — installing $SRC_VERSION (${RELEASE_ID})"

NEED_APT=""
for tool in python3 rsync curl ping; do
  command -v "$tool" >/dev/null 2>&1 || NEED_APT=1
done

# The interpreter that builds the venv. The dependency set needs Python 3.11+
# (websockets 17 dropped 3.10), so a bare `python3` is only acceptable when it
# is new enough; otherwise the newest versioned interpreter on the box wins.
# A clear refusal here beats pip's "no matching distribution" a minute later.
MIN_PY_MINOR=11
new_enough() { "$1" -c "import sys; sys.exit(0 if sys.version_info >= (3, $MIN_PY_MINOR) else 1)" 2>/dev/null; }
pick_python() {
  # The distro's python3 when it qualifies (its venv package is plain
  # python3-venv, and apt can install that); otherwise the newest versioned
  # interpreter, e.g. python3.12 from deadsnakes on Ubuntu 22.04.
  local c
  for c in python3 python3.13 python3.12 python3.11; do
    if command -v "$c" >/dev/null 2>&1 && new_enough "$c"; then command -v "$c"; return 0; fi
  done
  return 1
}
if ! PYTHON="$(pick_python)"; then
  if command -v python3 >/dev/null 2>&1; then
    HAVE="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo unknown)"
    echo >&2
    echo "  This panel needs Python 3.$MIN_PY_MINOR or newer; this host's python3 is $HAVE." >&2
    echo "  Ubuntu 24.04 ships 3.12. On Ubuntu 22.04 (3.10) install a newer interpreter first:" >&2
    echo "    sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.12 python3.12-venv" >&2
    echo "  then re-run this script; it picks up the newest python3.x it finds." >&2
    exit 1
  fi
  # No python at all: apt installs the distro's, checked again below.
  NEED_APT=1
  PYTHON=""
fi

# `import venv` succeeding does not mean a venv can be built: Ubuntu ships the
# module in the stdlib but the pip bootstrap wheels in python3-venv (or
# python3.X-venv for a versioned interpreter). The only honest test is to
# build one; when that fails and apt exists, the package is installed below.
VENV_PKG="python3-venv"
probe_venv() {
  local probe ok=1
  probe="$(mktemp -d)"
  if "$PYTHON" -m venv "$probe/v" >/dev/null 2>&1 && [[ -x "$probe/v/bin/pip" ]]; then ok=0; fi
  rm -rf "$probe"
  return $ok
}
if [[ -n "$PYTHON" ]]; then
  echo "  using $PYTHON ($("$PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])'))"
  VENV_PKG="$(basename "$PYTHON")-venv"
  if ! probe_venv; then
    command -v apt-get >/dev/null 2>&1 \
      || die "$PYTHON cannot create a virtualenv. Install its venv package ($VENV_PKG) and re-run."
    NEED_APT=1
  fi
fi

# Read the configured port with the panel's own parser when a release exists
# (so inline mappings and ${VAR} references are handled exactly as the server
# handles them), falling back to a plain scan on a first install.
read_configured_port() {
  [[ -f "$APP_DIR/config.yaml" ]] || return 1
  local py="$APP_DIR/current/venv/bin/python"
  if [[ -x "$py" ]]; then
    (cd "$APP_DIR/current" && PANEL_CONFIG="$APP_DIR/config.yaml" "$py" -c '
import os, sys
sys.path.insert(0, ".")
from app.config import Config
cfg = Config.load(os.environ["PANEL_CONFIG"])
port = cfg.get("server.port")
print(int(port) if port is not None else "")
' 2>/dev/null) && return 0
  fi
  awk '
    /^server:/       { in_server = 1; next }
    /^[^[:space:]#]/ { in_server = 0 }
    in_server && /^[[:space:]]+port:[[:space:]]*[0-9]+/ {
      gsub(/[^0-9]/, "", $2); if ($2 != "") { print $2; exit }
    }
  ' "$APP_DIR/config.yaml"
}

PORT_REQUESTED="${PANEL_PORT:-}"
if [[ -z "$PORT_REQUESTED" ]]; then
  PANEL_PORT="$(read_configured_port || true)"
  PANEL_PORT="${PANEL_PORT:-8080}"
fi
[[ "$PANEL_PORT" =~ ^[0-9]+$ ]] && (( PANEL_PORT >= 1 && PANEL_PORT <= 65535 )) \
  || die "PANEL_PORT must be a number between 1 and 65535, got '$PANEL_PORT'"

# Port conflict: exempt only OUR service's own process, identified by PID —
# not every listener that happens to be called uvicorn.
OUR_PID="$(sc show -p MainPID --value "$SERVICE" 2>/dev/null || echo 0)"
OUR_PID="${OUR_PID:-0}"
if command -v ss >/dev/null 2>&1; then
  CONFLICTS="$(ss -ltnpH 2>/dev/null | awk -v port=":${PANEL_PORT}" -v me="pid=${OUR_PID}," '
    $4 ~ port"$" && index($0, me) == 0 { print }')"
  if [[ -n "$CONFLICTS" ]]; then
    echo
    echo "  Port ${PANEL_PORT} is in use by something other than ${SERVICE}:"
    sed 's/^/    /' <<<"$CONFLICTS"
    echo
    echo "  Pick a free port:  sudo PANEL_PORT=8090 bash $SRC_DIR/install.sh"
    exit 1
  fi
  if (( OUR_PID > 0 )); then
    echo "  ${SERVICE} is running (pid ${OUR_PID}) on port ${PANEL_PORT} — will switch it over"
  else
    echo "  port ${PANEL_PORT} is free"
  fi
else
  echo "  ss not found; skipping the port check"
fi

AVAIL_MB="$(df -Pm /opt | awk 'NR==2 {print $4}')"
if [[ "${AVAIL_MB:-0}" -lt 600 ]]; then
  warn "only ${AVAIL_MB} MB free on /opt; a release with its venv needs roughly 250 MB"
fi

# ---------------------------------------------------------------------
# System packages (only when something is missing)
# ---------------------------------------------------------------------

if [[ -n "$NEED_APT" ]]; then
  say "Installing system packages"
  apt-get update -qq
  apt-get install -y -qq python3 "$VENV_PKG" python3-pip rsync curl iputils-ping
  if [[ -z "$PYTHON" ]]; then
    PYTHON="$(pick_python)" || die "apt installed python3 but it is older than 3.$MIN_PY_MINOR. Install python3.12 + python3.12-venv (deadsnakes on 22.04) and re-run."
    echo "  using $PYTHON"
  fi
  probe_venv || die "$PYTHON still cannot create a virtualenv after installing $VENV_PKG. Check: $PYTHON -m venv /tmp/probe"
fi

if ! id -u "$APP_USER" >/dev/null 2>&1; then
  say "Creating service account: $APP_USER"
  # No home directory: the unit sets ProtectHome=true, so it could never see one.
  useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$APP_USER"
fi

# ---------------------------------------------------------------------
# Layout, and migration from the old flat layout if present
# ---------------------------------------------------------------------

mkdir -p "$APP_DIR/releases" "$APP_DIR/data" "$BACKUP_ROOT"
chmod 755 "$APP_DIR" "$APP_DIR/releases"
chmod 700 "$BACKUP_ROOT"

if [[ -d "$APP_DIR/app" && ! -L "$APP_DIR/current" ]]; then
  say "Migrating from the old single-directory layout"
  LEGACY="$APP_DIR/releases/legacy-$(date +%Y%m%d%H%M%S)"
  mkdir -p "$LEGACY"
  chmod 755 "$LEGACY"   # umask 077 would make it unenterable for the service user
  for item in app web deploy venv install.sh requirements.txt config.example.yaml README.md; do
    [[ -e "$APP_DIR/$item" ]] && mv "$APP_DIR/$item" "$LEGACY/" || true
  done
  ln -sfn "$LEGACY" "$APP_DIR/current"
  echo "    old code moved to $LEGACY (kept for rollback)"
fi

# ---------------------------------------------------------------------
# Where to build — never inside a directory something still depends on
# ---------------------------------------------------------------------

PREVIOUS=""
if [[ -L "$APP_DIR/current" ]]; then
  PREVIOUS="$(readlink -f "$APP_DIR/current")"
fi
declare -A PROTECT=()
protected_releases PROTECT

REUSE_CURRENT=0
if [[ -d "$RELEASE_DIR" ]]; then
  REAL="$(readlink -f "$RELEASE_DIR")"
  if [[ -n "$PREVIOUS" && "$REAL" == "$PREVIOUS" ]]; then
    # This exact commit is what is running. Its files are not touched; at
    # most it is re-validated and re-activated below.
    REUSE_CURRENT=1
    if [[ -z "$PORT_REQUESTED" && -z "${PANEL_FORCE:-}" ]] \
       && PANELCTL_NO_SYSTEMD="$NO_SYSTEMD" "$APP_DIR/panelctl" wait-healthy 3 "$SRC_VERSION" >/dev/null 2>&1; then
      echo
      echo "  Release $RELEASE_ID is already installed, current and healthy. Nothing to do."
      echo "  (PANEL_FORCE=1 re-validates and restarts it without rebuilding.)"
      exit 0
    fi
    echo "  release $RELEASE_ID is already current — re-validating and restarting it, files untouched"
  elif [[ -n "${PROTECT[$REAL]:-}" ]]; then
    # A retained rollback target. Build this commit again, somewhere new.
    RELEASE_ID="${RELEASE_ID}-r$(date +%Y%m%d%H%M%S)"
    RELEASE_DIR="$APP_DIR/releases/$RELEASE_ID"
    echo "  a copy of this release is a rollback target; building a fresh one as $RELEASE_ID"
  else
    # An abandoned staging directory or a pruned-eligible old release.
    echo "  removing stale release directory $RELEASE_ID and rebuilding it"
    rm -rf "$RELEASE_DIR"
  fi
fi
echo

# ---------------------------------------------------------------------
# Build the new release in its own directory
# ---------------------------------------------------------------------

if (( ! REUSE_CURRENT )); then
  say "Staging release $RELEASE_ID"
  mkdir -p "$RELEASE_DIR"
  rsync -a --delete \
    --exclude '.git' --exclude '.github' --exclude 'tests' --exclude '.*_cache' \
    --exclude 'venv' --exclude '__pycache__' --exclude '*.pyc' --exclude 'node_modules' \
    --exclude 'data' --exclude 'config.yaml' --exclude '.env' --exclude 'backups' \
    "$SRC_DIR"/ "$RELEASE_DIR"/

  say "Building its virtualenv"
  "$PYTHON" -m venv "$RELEASE_DIR/venv"
  "$RELEASE_DIR/venv/bin/pip" install --quiet --upgrade pip
  if [[ -f "$RELEASE_DIR/requirements.lock" ]]; then
    "$RELEASE_DIR/venv/bin/pip" install --quiet -r "$RELEASE_DIR/requirements.lock"
  else
    "$RELEASE_DIR/venv/bin/pip" install --quiet -r "$RELEASE_DIR/requirements.txt"
  fi

  # Code is root-owned and world-readable (no secrets in it). Only state
  # belongs to the service account.
  chown -R root:root "$RELEASE_DIR"
  chmod -R u=rwX,go=rX "$RELEASE_DIR"
fi

# ---------------------------------------------------------------------
# Recovery point — before the first change to anything that exists
# ---------------------------------------------------------------------

BACKUP="${PANEL_BACKUP_DIR:-}"
if [[ -z "$BACKUP" && ( -f "$APP_DIR/config.yaml" || -f "$APP_DIR/data/panel.db" ) ]]; then
  say "Backing up the current state"
  make_backup "before-$SRC_VERSION" || die "Could not take a verified backup. Nothing was changed."
  BACKUP="$BACKUP_DEST"
  echo "    $BACKUP"
fi
if [[ -n "$BACKUP" ]]; then
  verify_backup "$BACKUP" || die "The backup at $BACKUP does not verify. Nothing was changed."
fi
[[ -n "${PANEL_BACKUP_OUT:-}" ]] && echo "$BACKUP" > "$PANEL_BACKUP_OUT"

# ---------------------------------------------------------------------
# Configuration (first run only creates; never overwrites)
# ---------------------------------------------------------------------

FIRST_RUN=0
if [[ ! -f "$APP_DIR/config.yaml" ]]; then
  FIRST_RUN=1
  # The starter config, not the full example: host metrics only, no
  # credentials, so a fresh install always comes up green. The example
  # enables collectors whose credentials expand to nothing on a new box.
  say "First run: creating config.yaml from the starter template"
  cp "$RELEASE_DIR/config.starter.yaml" "$APP_DIR/config.yaml"
fi
if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$RELEASE_DIR/deploy/env.example" "$APP_DIR/.env"
fi

# Apply the effective port to the config so the port we checked is the port
# the panel binds. Done with a YAML-aware rewrite that keeps comments. (The
# backup above holds the config as it was, so a failed activation undoes
# this too.)
CURRENT_PORT="$(read_configured_port || echo "")"
if [[ "$PANEL_PORT" != "${CURRENT_PORT:-8080}" ]]; then
  say "Setting server.port to $PANEL_PORT in config.yaml"
  "$RELEASE_DIR/venv/bin/python" - "$APP_DIR/config.yaml" "$PANEL_PORT" <<'PYEOF'
import re, sys
path, port = sys.argv[1], sys.argv[2]
lines = open(path, encoding="utf-8").read().splitlines(keepends=True)
out, in_server, done = [], False, False
for line in lines:
    if re.match(r"^server:\s*(#.*)?$", line):
        in_server = True
    elif re.match(r"^server:\s*\{", line):
        # inline mapping: server: {host: x, port: 8080}
        line = re.sub(r"(port:\s*)[0-9]+", r"\g<1>" + port, line); done = True
        in_server = False
    elif re.match(r"^[^\s#]", line):
        in_server = False
    if in_server and re.match(r"^\s+port:\s*[0-9]+", line):
        line = re.sub(r"(^\s+port:\s*)[0-9]+", r"\g<1>" + port, line); done = True
    out.append(line)
if not done:
    sys.stderr.write("could not find server.port in config.yaml; set it by hand\n"); sys.exit(1)
open(path, "w", encoding="utf-8").write("".join(out))
PYEOF
fi

chown -R "$APP_USER:$APP_USER" "$APP_DIR/data"
chown "$APP_USER:$APP_USER" "$APP_DIR/config.yaml" "$APP_DIR/.env"
chmod 600 "$APP_DIR/config.yaml" "$APP_DIR/.env"
chmod 750 "$APP_DIR/data"

# ---------------------------------------------------------------------
# Validate the NEW release against YOUR config, before anything changes
# ---------------------------------------------------------------------

# Validation runs the staged release's own wrapper against the staged code,
# as the service user, with the service's environment. The deployed wrapper
# at $APP_DIR/panelctl is not replaced until activation, so a rejected
# release leaves no new control files behind.
# Through bash: a checkout committed from Windows may have lost the file's
# executable bit, and that must not fail an update.
say "Validating configuration against $SRC_VERSION"
if ! PANELCTL_RELEASE="$RELEASE_DIR" PANELCTL_NO_SYSTEMD="$NO_SYSTEMD" bash "$RELEASE_DIR/deploy/panelctl" check; then
  echo
  if [[ -n "$PREVIOUS" ]]; then
    echo "  The new release does not accept the current configuration." >&2
    echo "  The running release was NOT changed and is still serving." >&2
    (( REUSE_CURRENT )) || echo "  Staged (unused) release left at: $RELEASE_DIR" >&2
  else
    echo "  The configuration is not valid, so the service was NOT started." >&2
    echo "  Nothing is running and nothing was broken. Fix the items above:" >&2
    echo "    sudo nano $APP_DIR/.env          # credentials" >&2
    echo "    sudo nano $APP_DIR/config.yaml   # what to watch" >&2
    echo "    sudo bash $SRC_DIR/install.sh    # then run this again" >&2
  fi
  exit 1
fi

# ---------------------------------------------------------------------
# Activate: the one disruptive step, and its undo
# ---------------------------------------------------------------------

# The manifest describes what is DEPLOYED, so it is written only once the
# new release has proven healthy. Writing it before the switch meant a failed
# update left a manifest naming a release that was not running — update.sh
# then believed the update had already happened and refused to retry.
say "Activating $RELEASE_ID"
MANIFEST_PREVIOUS="$PREVIOUS"
if (( REUSE_CURRENT )); then
  # Re-activating what is already current: the previous release is still
  # whatever the deployed manifest says, not this release itself.
  MANIFEST_PREVIOUS="$(jget "$APP_DIR/manifest.json" previous_release_dir)"
fi
"$RELEASE_DIR/venv/bin/python" - "$APP_DIR/manifest.json.next" "$SRC_VERSION" "$RELEASE_ID" "$SRC_COMMIT" "$RELEASE_DIR" "$MANIFEST_PREVIOUS" "$SRC_DIR" <<'PYEOF'
import json, sys, time, os
path, version, release_id, commit, release_dir, previous, source_dir = sys.argv[1:8]
sys.path.insert(0, release_dir)
from app.version import SCHEMA_VERSION, CONFIG_VERSION
manifest = {
    "version": version,
    "release_id": release_id,
    "commit": commit or None,
    "release_dir": release_dir,
    "previous_release_dir": previous or None,
    "source_dir": source_dir,
    "schema_version": SCHEMA_VERSION,
    "config_version": CONFIG_VERSION,
    "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
}
with open(path, "w", encoding="utf-8") as fh:
    json.dump(manifest, fh, indent=2)
PYEOF
chmod 644 "$APP_DIR/manifest.json.next"

revert() {
  # Undo the activation. Called from every failure path after the first
  # change to running state, so "if it switched, it reverted" holds without
  # exceptions. Order matters: the service must be provably stopped before
  # the database is touched, and the database must go back before the old
  # release starts (a migrated database is one it refuses to open).
  rm -f "$APP_DIR/manifest.json.next"
  echo >&2
  echo "  Reverting." >&2
  if ! stop_confirmed; then
    echo "  Could not confirm that $SERVICE stopped, so nothing on disk was touched." >&2
    echo "  Stop it by hand, then:  sudo $APP_DIR/panelctl update --rollback" >&2
    return 0
  fi
  if [[ -n "$BACKUP" ]]; then
    if restore_state "$BACKUP"; then
      echo "  Restored database, config, .env, manifest and control files from $(basename "$BACKUP")." >&2
    else
      echo "  Restoring from $BACKUP FAILED — inspect it before starting anything." >&2
      return 0
    fi
  fi
  if [[ -n "$PREVIOUS" && -d "$PREVIOUS" ]]; then
    switch_symlink "$PREVIOUS"
    if (( NO_SYSTEMD )); then
      start_direct "$PREVIOUS"
    else
      systemctl daemon-reload || true
      sc_start start "$SERVICE" || true
    fi
    if PANELCTL_NO_SYSTEMD="$NO_SYSTEMD" "$APP_DIR/panelctl" wait-healthy 30 >/dev/null 2>&1; then
      echo "  Previous release is serving again." >&2
    else
      echo "  The previous release did not come back either — inspect the journal." >&2
    fi
    stop_direct
  else
    echo "  There was no previous release to fall back to." >&2
    # A first install that never became healthy must not leave a unit that
    # crash-loops on the next boot.
    sc disable "$SERVICE" >/dev/null 2>&1 || true
    echo "  The service is stopped and disabled; nothing runs until this succeeds." >&2
  fi
}

# Control files travel with the release they belong to. The old ones are in
# the backup and come back on revert.
install -m 755 "$RELEASE_DIR/deploy/panelctl" "$APP_DIR/panelctl"
# On PATH for root, so `sudo panelctl check` works exactly as the README says.
ln -sfn "$APP_DIR/panelctl" /usr/local/bin/panelctl 2>/dev/null || true

switch_symlink "$RELEASE_DIR"

if (( NO_SYSTEMD )); then
  # Start the release directly so the health check has something to talk to.
  start_direct "$RELEASE_DIR"
else
  if ! install -m 644 "$RELEASE_DIR/deploy/homelab-panel.service" "$UNIT_PATH" \
     || ! systemctl daemon-reload \
     || ! systemctl enable "$SERVICE" >/dev/null 2>&1 \
     || ! sc_start restart "$SERVICE"; then
    echo >&2
    echo "  Could not (re)start $SERVICE." >&2
    journalctl -u "$SERVICE" --no-pager --lines=15 | sed 's/^/    /' >&2 || true
    revert
    exit 1
  fi
fi

# ---------------------------------------------------------------------
# Verify it actually came up, not merely that the process exists
# ---------------------------------------------------------------------

say "Verifying"
if ! PANELCTL_NO_SYSTEMD="$NO_SYSTEMD" "$APP_DIR/panelctl" wait-healthy 45 "$SRC_VERSION"; then
  echo >&2
  echo "  $SERVICE did not become healthy. Last log lines:" >&2
  (( NO_SYSTEMD )) || journalctl -u "$SERVICE" --no-pager --lines=25 | sed 's/^/    /' >&2 || true
  revert
  exit 1
fi

# Healthy: the manifest becomes the truth.
mv -f "$APP_DIR/manifest.json.next" "$APP_DIR/manifest.json"
stop_direct

# Keep the last few releases for rollback, drop the rest — but never one that
# a complete backup still points to, or --rollback would find its release gone.
declare -A KEEP=()
protected_releases KEEP
mapfile -t OLD < <(ls -1dt "$APP_DIR"/releases/*/ 2>/dev/null | tail -n +$((KEEP_RELEASES + 1)))
for dir in "${OLD[@]}"; do
  real="$(readlink -f "$dir")"
  if [[ -z "${KEEP[$real]:-}" ]]; then
    rm -rf "$dir"
  fi
done

HOST_IP="$( (hostname -I 2>/dev/null || true) | awk '{print $1}')"
HOST_IP="${HOST_IP:-localhost}"
FINAL_PORT="$(read_configured_port || echo "$PANEL_PORT")"

if (( FIRST_RUN )); then
cat <<EOF

Installed and running $SRC_VERSION — with host metrics only.

  Open the panel      http://${HOST_IP}:${FINAL_PORT}

That board is real but nearly empty, which is the point: it proves the
install works before any credentials are involved. To add your lab:

  1. Put credentials in    sudo nano $APP_DIR/.env
  2. Copy the blocks you want from
                           $APP_DIR/current/config.example.yaml
     into                  sudo nano $APP_DIR/config.yaml
  3. Check it              sudo panelctl check
  4. Apply                 sudo panelctl restart

Add two or three collectors at a time and check between each.

The API has no token yet — fine on a trusted LAN or behind Cloudflare Access.
To require one:   sudo panelctl gen-token   then set server.api_token.

  Follow logs         sudo panelctl logs
  Update later        sudo panelctl update

EOF
else
cat <<EOF

Installed and running $SRC_VERSION (release $RELEASE_ID).

  Check config        sudo panelctl check
  Apply changes       sudo panelctl restart
  Follow logs         sudo panelctl logs
  What's deployed     sudo panelctl status
  Update              sudo panelctl update
  Open the panel      http://${HOST_IP}:${FINAL_PORT}
${BACKUP:+  Recovery point      $BACKUP
}
EOF
fi
