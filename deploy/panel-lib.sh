# shellcheck shell=bash
#
# panel-lib.sh — the parts of install.sh and update.sh that must agree.
#
# Sourced, not executed. Everything an install, an update and a rollback
# have in common lives here so the three cannot drift apart:
#
#   * one lock for the whole transaction (install called from update
#     inherits it rather than fighting for it)
#   * one definition of a backup, and one of a verified backup
#   * one way to stop the service and PROVE it stopped before SQLite files
#     are touched
#   * one way to put a backup's state back
#
# Callers set APP_DIR, BACKUP_ROOT, SERVICE, APP_USER and NO_SYSTEMD before
# sourcing. All functions return non-zero on failure and never exit, except
# `die`, so the caller decides what a failure means.

say()  { echo "==> $*"; }
warn() { echo "    ! $*" >&2; }
die()  { echo >&2; echo "  $*" >&2; exit 1; }

# systemctl, or nothing at all under PANEL_NO_SYSTEMD=1 (CI in a container).
# "true"/"yes" are accepted too: `(( true ))` is 0 in bash, which would have
# silently meant "use systemd".
case "${NO_SYSTEMD:-0}" in 1|true|yes|on) NO_SYSTEMD=1 ;; *) NO_SYSTEMD=0 ;; esac
sc() { if (( NO_SYSTEMD )); then return 0; fi; systemctl "$@"; }

sc_start() {
  # A release that crashed on startup was restarted by systemd every 5s
  # during the health wait and has spent the unit's start-rate budget
  # (StartLimitBurst). `systemctl stop` does not reset that counter, so the
  # start that puts the PREVIOUS release back would be refused with
  # start-limit-hit and both releases would be down. Reset it first.
  (( NO_SYSTEMD )) && return 0
  systemctl reset-failed "$SERVICE" 2>/dev/null || true
  systemctl "$@"
}

# JSON field from a file, with the system python (always present).
jget() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); v=d.get(sys.argv[2]); print("" if v is None else v)' "$1" "$2" 2>/dev/null || true; }

LOCK_FILE="${LOCK_FILE:-/run/lock/homelab-panel.lock}"
UNIT_PATH="/etc/systemd/system/${SERVICE}.service"

# ---------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------

take_lock() {
  # update.sh holds the lock and runs install.sh inside it: the child sees
  # PANEL_LOCK_HELD and does not try to take it again.
  if [[ -n "${PANEL_LOCK_HELD:-}" ]]; then return 0; fi
  mkdir -p "$(dirname "$LOCK_FILE")"
  exec 9>"$LOCK_FILE"
  flock -n 9 || die "Another install, update or rollback is running (lock: $LOCK_FILE)."
  export PANEL_LOCK_HELD=1
}

# ---------------------------------------------------------------------
# Stopping the service — and knowing that it stopped
# ---------------------------------------------------------------------

# DIRECT_PID is the directly-started process under NO_SYSTEMD.
DIRECT_PID="${DIRECT_PID:-}"

start_direct() {
  # $1 is the release directory to start (defaults to current).
  (( NO_SYSTEMD )) || return 0
  local rel="${1:-$APP_DIR/current}"
  ( cd "$rel" && exec sudo -u "$APP_USER" env \
      PANEL_CONFIG="$APP_DIR/config.yaml" PANEL_DB="$APP_DIR/data/panel.db" PANEL_LOG_LEVEL=warning \
      "$rel/venv/bin/python" -m app.main ) &
  DIRECT_PID=$!
}

stop_direct() {
  [[ -n "$DIRECT_PID" ]] || return 0
  kill "$DIRECT_PID" 2>/dev/null || true
  wait "$DIRECT_PID" 2>/dev/null || true
  DIRECT_PID=""
}

stop_confirmed() {
  # Stop the service and return 0 only once no process of it remains. A
  # database restored underneath a live writer is how data gets lost, so
  # every caller that touches SQLite files requires this to succeed first.
  if (( NO_SYSTEMD )); then
    stop_direct
    return 0
  fi
  systemctl stop "$SERVICE" 2>/dev/null || true
  local i state pid
  for (( i = 0; i < 30; i++ )); do
    state="$(systemctl is-active "$SERVICE" 2>/dev/null || true)"
    pid="$(systemctl show -p MainPID --value "$SERVICE" 2>/dev/null || echo 0)"
    if [[ "$state" != "active" && "$state" != "deactivating" && "${pid:-0}" == "0" ]]; then
      return 0
    fi
    sleep 1
  done
  warn "$SERVICE did not stop within 30s (state: ${state:-?}, pid: ${pid:-?})"
  return 1
}

# ---------------------------------------------------------------------
# Symlink
# ---------------------------------------------------------------------

switch_symlink() {
  # Atomic: build the new link beside the old one, then rename over it.
  ln -sfn "$1" "$APP_DIR/current.new" && mv -Tf "$APP_DIR/current.new" "$APP_DIR/current"
}

# ---------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------
#
# A backup directory holds:
#   FILES         the names that were copied (one per line) — the contract
#   SHA256SUMS    over exactly those names, .env included
#   COMPLETE      written last, atomically, only after the sums re-verify
# Anything without COMPLETE is not a backup, and anything whose sums do not
# verify is not one either, however the marker got there.

# Set by make_backup; read by the sourcing script.
BACKUP_DEST=""

_backup_copy() {
  # _backup_copy <src> <dest-dir> <name>: copy if the source exists, and
  # record the name. A source that exists but cannot be copied is a failure.
  local src="$1" dest="$2" name="$3"
  [[ -e "$src" ]] || return 0
  cp -p -- "$src" "$dest/$name" || { warn "could not copy $src"; return 1; }
  echo "$name" >> "$dest/FILES" || return 1
}

make_backup() {
  # make_backup <label>  — sets BACKUP_DEST on success, returns 1 on any
  # failure. Deliberately NOT designed to run in $(...): command substitution
  # drops errexit, which is exactly how a half-written backup once got its
  # COMPLETE marker.
  local label="${1:-manual}" stamp id dest n=1
  BACKUP_DEST=""
  stamp="$(date +%Y%m%d-%H%M%S)"
  id="${stamp}-${label}"
  dest="$BACKUP_ROOT/$id"
  # Two operations in the same second must not share a directory.
  while [[ -e "$dest" ]]; do dest="$BACKUP_ROOT/$id-$((n++))"; done
  mkdir -p "$dest" || return 1
  chmod 700 "$BACKUP_ROOT" "$dest" || return 1
  : > "$dest/FILES" || return 1

  _backup_copy "$APP_DIR/manifest.json" "$dest" manifest.json || return 1
  _backup_copy "$APP_DIR/config.yaml"   "$dest" config.yaml   || return 1
  _backup_copy "$APP_DIR/.env"          "$dest" .env          || return 1
  _backup_copy "$APP_DIR/panelctl"      "$dest" panelctl      || return 1
  _backup_copy "$UNIT_PATH"             "$dest" homelab-panel.service || return 1

  if [[ -f "$APP_DIR/data/panel.db" ]]; then
    # SQLite's online backup API, not cp: a copy of a live WAL database can
    # be torn. This is consistent even mid-write. The system python has
    # sqlite3, so this does not depend on any release's venv.
    python3 - "$APP_DIR/data/panel.db" "$dest/panel.db" <<'PYEOF' || { warn "database backup failed"; return 1; }
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
target = sqlite3.connect(dst)
with target:
    source.backup(target)
source.close(); target.close()
check = sqlite3.connect(dst)
ok = check.execute("PRAGMA integrity_check").fetchone()[0]
check.close()
if ok != "ok":
    sys.stderr.write(f"backup failed integrity check: {ok}\n"); sys.exit(1)
PYEOF
    echo "panel.db" >> "$dest/FILES" || return 1
  fi

  # Hash exactly the recorded names (dotfiles included), then prove the
  # hashes read back before anyone is allowed to trust them.
  ( cd "$dest" && xargs -d '\n' -a FILES sha256sum -- > SHA256SUMS ) || { warn "could not write checksums"; return 1; }
  ( cd "$dest" && sha256sum --quiet -c SHA256SUMS ) >/dev/null 2>&1 || { warn "checksums did not verify"; return 1; }
  chmod -R go-rwx "$dest" || return 1

  date -u +%Y-%m-%dT%H:%M:%SZ > "$dest/COMPLETE.tmp" && mv -f "$dest/COMPLETE.tmp" "$dest/COMPLETE" || return 1
  # shellcheck disable=SC2034  # read by install.sh / update.sh after sourcing
  BACKUP_DEST="$dest"
  return 0
}

verify_backup() {
  # verify_backup <dir>: is this something we would restore? Prints why not.
  local dir="$1"
  [[ -d "$dir" ]]            || { warn "$dir is not a directory"; return 1; }
  [[ -f "$dir/COMPLETE" ]]   || { warn "$(basename "$dir") is incomplete"; return 1; }
  [[ -f "$dir/FILES" ]]      || { warn "$(basename "$dir") has no file list (made by an older updater); restore it by hand"; return 1; }
  [[ -f "$dir/SHA256SUMS" ]] || { warn "$(basename "$dir") has no checksums"; return 1; }
  ( cd "$dir" && sha256sum --quiet -c SHA256SUMS ) >/dev/null 2>&1 \
    || { warn "$(basename "$dir") fails checksum verification — it has been altered or damaged"; return 1; }
  if [[ -f "$dir/panel.db" ]]; then
    python3 - "$dir/panel.db" <<'PYEOF' || { warn "$(basename "$dir")/panel.db fails integrity_check"; return 1; }
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
ok = c.execute("PRAGMA integrity_check").fetchone()[0]
c.close()
sys.exit(0 if ok == "ok" else 1)
PYEOF
  fi
  return 0
}

backup_has() { [[ -f "$1/FILES" ]] && grep -qx -- "$2" "$1/FILES"; }

restore_state() {
  # restore_state <backup-dir>: put back every file the backup recorded.
  # The caller MUST have confirmed the service is stopped. Verifies first
  # so a damaged backup changes nothing.
  local b="$1"
  verify_backup "$b" || return 1
  if backup_has "$b" panel.db; then
    mkdir -p "$APP_DIR/data" || return 1
    cp -p -- "$b/panel.db" "$APP_DIR/data/panel.db.restore" || return 1
    mv -f -- "$APP_DIR/data/panel.db.restore" "$APP_DIR/data/panel.db" || return 1
    rm -f -- "$APP_DIR/data/panel.db-wal" "$APP_DIR/data/panel.db-shm"
    chown "$APP_USER:$APP_USER" "$APP_DIR/data/panel.db" 2>/dev/null || true
  fi
  if backup_has "$b" config.yaml; then
    cp -p -- "$b/config.yaml" "$APP_DIR/config.yaml" || return 1
    chown "$APP_USER:$APP_USER" "$APP_DIR/config.yaml" 2>/dev/null || true
    chmod 600 "$APP_DIR/config.yaml"
  fi
  if backup_has "$b" .env; then
    cp -p -- "$b/.env" "$APP_DIR/.env" || return 1
    chown "$APP_USER:$APP_USER" "$APP_DIR/.env" 2>/dev/null || true
    chmod 600 "$APP_DIR/.env"
  fi
  if backup_has "$b" manifest.json; then
    cp -p -- "$b/manifest.json" "$APP_DIR/manifest.json" || return 1
    chmod 644 "$APP_DIR/manifest.json"
  fi
  if backup_has "$b" panelctl; then
    install -m 755 -- "$b/panelctl" "$APP_DIR/panelctl" || return 1
  fi
  if backup_has "$b" homelab-panel.service && (( ! NO_SYSTEMD )); then
    install -m 644 -- "$b/homelab-panel.service" "$UNIT_PATH" || return 1
    systemctl daemon-reload || true
  fi
  return 0
}

latest_complete_backup() {
  local -a dirs=()
  local dir
  mapfile -t dirs < <(ls -1dt "$BACKUP_ROOT"/*/ 2>/dev/null)
  for dir in "${dirs[@]}"; do
    [[ -f "$dir/COMPLETE" ]] && { echo "${dir%/}"; return 0; }
  done
  return 1
}

# ---------------------------------------------------------------------
# Which release directories must never be deleted or rebuilt
# ---------------------------------------------------------------------

# protected_releases <assoc-array-name>: fills it with the real paths of the
# current release, the recorded previous release, and every release a
# complete backup points at.
protected_releases() {
  local -n _out="$1"
  local m ref prev
  if [[ -L "$APP_DIR/current" ]]; then _out["$(readlink -f "$APP_DIR/current")"]=1; fi
  prev="$(jget "$APP_DIR/manifest.json" previous_release_dir)"
  [[ -n "$prev" ]] && _out["$(readlink -f "$prev" 2>/dev/null || echo "$prev")"]=1
  for m in "$BACKUP_ROOT"/*/manifest.json; do
    [[ -f "$m" && -f "$(dirname "$m")/COMPLETE" ]] || continue
    ref="$(jget "$m" release_dir)"
    [[ -n "$ref" ]] && _out["$(readlink -f "$ref" 2>/dev/null || echo "$ref")"]=1
  done
  return 0
}
