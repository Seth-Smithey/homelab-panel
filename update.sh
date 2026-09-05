#!/usr/bin/env bash
#
# homelab panel — updater
#
#   sudo bash update.sh                   update to the newest stable release
#   sudo bash update.sh --channel main    track the main branch instead
#   sudo bash update.sh --channel pre     include pre-releases (v1.2.0-rc.1)
#   sudo bash update.sh --to v1.4.2       a specific version
#   sudo bash update.sh --check           say what would happen; change nothing
#   sudo bash update.sh --list-backups    what can be rolled back to
#   sudo bash update.sh --rollback [ID]   undo the last update, or a specific backup
#   (--allow-major / --allow-downgrade to cross a major version or move to an older build)
#
# What it relies on, and why it can be trusted at 2am:
#
#   * It reads what is DEPLOYED from /opt/homelab-panel/manifest.json — written
#     by install.sh at activation — never from the source checkout, which may
#     have been advanced without being installed.
#   * It runs from a temporary copy of itself. The checkout it updates may
#     replace this very file, and Bash reads scripts incrementally.
#   * The backup goes to /var/backups/homelab-panel with an explicit file
#     list, checksums over that list and a completion marker written last.
#     Nothing that fails verification is ever a rollback candidate.
#   * Nothing running is touched until the new release has been built in its
#     own directory and validated against YOUR config. Then one symlink flip
#     and one restart. If that fails, the database, config, .env, wrapper
#     and unit come back from the backup and the previous release restarts.
#   * Rollback proves the service has stopped before it touches SQLite files,
#     restores the recorded release directory (still on disk, its venv
#     intact — no network needed) plus the state, and verifies health
#     before it says anything succeeded.
#   * One lock for the whole transaction, shared with install.sh.

set -euo pipefail

# shellcheck disable=SC2034  # consumed by deploy/panel-lib.sh (start_direct, restore_state)
APP_USER="panel"
APP_DIR="/opt/homelab-panel"
BACKUP_ROOT="/var/backups/homelab-panel"
SERVICE="homelab-panel"
KEEP_BACKUPS=5
HEALTH_TIMEOUT=60

# ---------------------------------------------------------------------
# Run from a copy. The rest of this file executes from a temp directory.
# ---------------------------------------------------------------------

if [[ -z "${PANEL_UPDATE_RUNNER:-}" ]]; then
  ORIG_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  RUNNER="$(mktemp -d /tmp/homelab-panel-update.XXXXXX)"
  mkdir -p "$RUNNER/deploy"
  cp "$ORIG_SRC/update.sh" "$RUNNER/update.sh"
  cp "$ORIG_SRC/deploy/panel-lib.sh" "$RUNNER/deploy/panel-lib.sh"
  export PANEL_UPDATE_RUNNER="$RUNNER" PANEL_UPDATE_SRC_DIR="$ORIG_SRC"
  exec bash "$RUNNER/update.sh" "$@"
fi
SRC_DIR="$PANEL_UPDATE_SRC_DIR"
trap 'rm -rf "$PANEL_UPDATE_RUNNER"' EXIT

CHANNEL="stable"
TARGET=""
CHECK_ONLY=0
ALLOW_MAJOR=0
ALLOW_DOWNGRADE=0
DO_ROLLBACK=0
ROLLBACK_ID=""
LIST_BACKUPS=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --channel)     CHANNEL="${2:-stable}"; shift 2 ;;
    --to)          TARGET="${2:-}"; CHANNEL="pinned"; shift 2 ;;
    --check)       CHECK_ONLY=1; shift ;;
    --allow-major) ALLOW_MAJOR=1; shift ;;
    --allow-downgrade) ALLOW_DOWNGRADE=1; shift ;;
    --rollback)    DO_ROLLBACK=1; shift
                   if [[ $# -gt 0 && "$1" != --* ]]; then ROLLBACK_ID="$1"; shift; fi ;;
    --list-backups) LIST_BACKUPS=1; shift ;;
    -h|--help)     sed -n '2,31p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "Run this with sudo." >&2; exit 1; }
umask 077

# PANEL_NO_SYSTEMD=1 mirrors install.sh's CI mode: unit management becomes a
# no-op and the release is started directly for the health check.
NO_SYSTEMD="${PANEL_NO_SYSTEMD:-0}"

# shellcheck source=deploy/panel-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/deploy/panel-lib.sh"

ctl() { PANELCTL_NO_SYSTEMD="$NO_SYSTEMD" "$APP_DIR/panelctl" "$@"; }

take_lock

# ---------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------

list_backups() {
  local dir id state
  echo "Backups in $BACKUP_ROOT (newest first):"
  local -a dirs=()
  mapfile -t dirs < <(ls -1dt "$BACKUP_ROOT"/*/ 2>/dev/null)
  for dir in "${dirs[@]}"; do
    id="$(basename "$dir")"
    if [[ ! -f "$dir/COMPLETE" ]]; then
      state="INCOMPLETE — not usable"
    elif verify_backup "$dir" 2>/dev/null; then
      state="complete, verified"
    else
      state="DAMAGED — fails verification"
    fi
    printf '  %-36s  v%-14s release %-16s %s\n' "$id" \
      "$(jget "$dir/manifest.json" version)" "$(jget "$dir/manifest.json" release_id)" "$state"
  done
}

prune_backups() {
  # Never delete an incomplete one automatically — a person should see it.
  # Written with if/then rather than `[[ ]] && rm`: an AND-list whose test
  # fails on the last iteration makes this function return 1, and under
  # `set -e` that silently killed the script *after* a successful update.
  local -a old=()
  mapfile -t old < <(ls -1dt "$BACKUP_ROOT"/*/ 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)))
  local dir
  for dir in "${old[@]}"; do
    if [[ -n "$dir" && -f "$dir/COMPLETE" ]]; then
      rm -rf "$dir"
    fi
  done
  return 0
}

# ---------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------

do_rollback() {
  local backup target_dir target_version taken
  if [[ -n "$ROLLBACK_ID" ]]; then
    backup="$BACKUP_ROOT/$ROLLBACK_ID"
    [[ -d "$backup" ]] || die "No backup named $ROLLBACK_ID. Try --list-backups."
  else
    backup="$(latest_complete_backup)" || die "No complete backup found under $BACKUP_ROOT."
  fi
  # Everything about the backup is checked before anything is changed.
  verify_backup "$backup" || die "Backup $(basename "$backup") cannot be trusted; nothing was changed."

  target_dir="$(jget "$backup/manifest.json" release_dir)"
  target_version="$(jget "$backup/manifest.json" version)"
  taken="$(cat "$backup/COMPLETE" 2>/dev/null || true)"
  [[ -n "$target_dir" ]] || die "Backup has no recorded release directory. It predates manifests; restore by hand."
  [[ -d "$target_dir" && -x "$target_dir/venv/bin/python" ]] \
    || die "The release recorded in the backup ($target_dir) is no longer on disk. Reinstall that version from git, then restore $backup/panel.db by hand."

  echo
  echo "  Roll back to   v$target_version  ($target_dir)"
  echo "  From backup    $(basename "$backup")  taken $taken"
  echo "  This discards  every reading, event and mute recorded since then."
  echo
  if (( CHECK_ONLY )); then
    echo "  --check: nothing changed."
    exit 0
  fi

  # A rollback discards whatever happened since the backup. Snapshot that
  # first so a rollback is itself reversible.
  say "Snapshotting the current state before rolling back"
  make_backup "pre-rollback" || die "Could not take a verified snapshot of the current state. Nothing was changed."
  local safety="$BACKUP_DEST"
  echo "    $safety"

  say "Stopping $SERVICE"
  stop_confirmed || die "Could not confirm that $SERVICE stopped. Refusing to restore a database under a running service; nothing was changed."

  say "Restoring state from $(basename "$backup")"
  restore_state "$backup" || die "Restore failed part-way. The service is stopped. Inspect $backup and $APP_DIR before starting it."

  say "Switching to $target_dir"
  switch_symlink "$target_dir"
  if (( NO_SYSTEMD )); then
    start_direct "$target_dir"
  else
    systemctl daemon-reload || true
    sc_start start "$SERVICE" || true
  fi

  say "Verifying"
  if ctl wait-healthy "$HEALTH_TIMEOUT" "$target_version"; then
    stop_direct
    say "Rolled back to v$target_version."
    echo "    The state you rolled back from is saved at $safety"
    echo "    Note: running --rollback again restores THAT snapshot (i.e. undoes this rollback)."
    echo "    To go further back:  --list-backups, then --rollback <id>"
    exit 0
  fi
  stop_direct
  (( NO_SYSTEMD )) || journalctl -u "$SERVICE" --no-pager --lines=25 | sed 's/^/    /' >&2 || true
  die "The restored release did not become healthy. Its files are in place; inspect the journal above."
}

(( LIST_BACKUPS )) && { list_backups; exit 0; }
[[ -x "$APP_DIR/panelctl" && -L "$APP_DIR/current" ]] \
  || die "No installed panel at $APP_DIR (or it predates the release layout). Run install.sh first."
mkdir -p "$BACKUP_ROOT"; chmod 700 "$BACKUP_ROOT"
(( DO_ROLLBACK )) && do_rollback

# ---------------------------------------------------------------------
# What is deployed, and what is available
# ---------------------------------------------------------------------

# git as the checkout's owner, repairing anything an older install left
# root-owned (see deploy/panel-lib.sh).
init_git_owner "$SRC_DIR"

g rev-parse --git-dir >/dev/null 2>&1 \
  || die "$SRC_DIR is not a git checkout. Updating in place needs one:
    git clone https://github.com/Seth-Smithey/homelab-panel.git
  then run install.sh from the clone."

# Mode-only differences are ignored: a `chmod +x install.sh` on a clone whose
# index says 644 is not a local change worth refusing over.
if [[ -n "$(g -c core.fileMode=false status --porcelain 2>/dev/null)" ]]; then
  warn "You have local changes in $SRC_DIR:"
  g -c core.fileMode=false status --short | sed 's/^/      /'
  die "Refusing to update from a dirty checkout. Commit, stash, or re-clone."
fi

# The path to version.py relative to the repository root — this checkout may
# be the whole repo or a subdirectory of a larger one. Never assume root.
REPO_PREFIX="$(g rev-parse --show-prefix 2>/dev/null || true)"
VERSION_PATH="${REPO_PREFIX}app/version.py"

command -v curl >/dev/null 2>&1 || die "curl is required for the health check (apt install curl)."

say "Fetching"
if ! FETCH_ERR="$(g fetch --tags --quiet origin 2>&1)"; then
  # Git's own message names the cause — a DNS failure, a credential prompt,
  # a proxy, an ownership refusal. Swallowing it left only "could not reach
  # the remote", which sends people looking at the network for an hour.
  [[ -n "$FETCH_ERR" ]] && sed 's/^/      /' <<<"$FETCH_ERR" >&2
  die "Could not reach the remote (git's error is above)."
fi

if [[ -f "$APP_DIR/manifest.json" ]]; then
  CURRENT_VERSION="$(jget "$APP_DIR/manifest.json" version)"
  CURRENT_COMMIT="$(jget "$APP_DIR/manifest.json" commit)"
else
  CURRENT_VERSION="$(ctl version 2>/dev/null || echo 0.0.0)"
  CURRENT_COMMIT=""
fi

# versionsort.suffix=- makes v1.0.0-rc.2 sort BELOW v1.0.0 (git's default puts
# a suffixed tag above the bare one, which would offer the rc as an "update"
# to someone already on the release).
stable_tags() { g -c versionsort.suffix=- tag -l 'v*' --sort=-v:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' || true; }
all_tags()    { g -c versionsort.suffix=- tag -l 'v*' --sort=-v:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$' || true; }

case "$CHANNEL" in
  stable) TARGET="$(stable_tags | head -1)"
          if [[ -z "$TARGET" ]]; then
            say "No stable release (vX.Y.Z) has been tagged yet, so there is nothing to update to."
            echo "    Pre-releases: sudo panelctl update --channel pre     Branch: --channel main"
            exit 0
          fi ;;
  pre)    TARGET="$(all_tags | head -1)"
          if [[ -z "$TARGET" ]]; then
            say "No release has been tagged yet, so there is nothing to update to."
            echo "    To track the branch instead: sudo panelctl update --channel main"
            exit 0
          fi ;;
  main)   TARGET="origin/main" ;;
  pinned) [[ -n "$TARGET" ]] || die "--to needs a version."
          g rev-parse --verify --quiet "$TARGET^{commit}" >/dev/null || die "No such tag or ref: $TARGET" ;;
  *)      die "Unknown channel: $CHANNEL (stable | pre | main)" ;;
esac

TARGET_COMMIT="$(g rev-parse --short=12 "$TARGET^{commit}")"
NEW_VERSION="$(g show "$TARGET:$VERSION_PATH" 2>/dev/null \
  | sed -n 's/^__version__ = "\(.*\)"/\1/p' | head -1)"
# Missing or malformed target metadata aborts BEFORE anything is checked out.
[[ "$NEW_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]] \
  || die "Could not read a valid version from $TARGET:$VERSION_PATH (got '${NEW_VERSION:-nothing}'). Refusing to guess."

echo
echo "  Deployed    ${CURRENT_VERSION:-unknown}  ${CURRENT_COMMIT:+(${CURRENT_COMMIT})}"
echo "  Available   $NEW_VERSION  ($TARGET @ $TARGET_COMMIT)"
echo

if [[ "$TARGET_COMMIT" == "$CURRENT_COMMIT" ]]; then
  say "Already running exactly this commit."
  exit 0
fi

IS_MAJOR=0
CUR_MAJOR="${CURRENT_VERSION%%.*}"; NEW_MAJOR="${NEW_VERSION%%.*}"
if [[ "$CUR_MAJOR" =~ ^[0-9]+$ && "$NEW_MAJOR" =~ ^[0-9]+$ ]] && (( NEW_MAJOR > CUR_MAJOR )); then
  IS_MAJOR=1
fi
# A target that is an ancestor of what is deployed is an older build. That is
# what --rollback is for (it restores the matching database too); doing it
# through update runs a newer schema against older code and gets refused by
# the migration guard at best.
IS_DOWNGRADE=0
if [[ -n "$CURRENT_COMMIT" ]] && g cat-file -e "$CURRENT_COMMIT^{commit}" 2>/dev/null \
   && g merge-base --is-ancestor "$TARGET_COMMIT" "$CURRENT_COMMIT" 2>/dev/null; then
  IS_DOWNGRADE=1
fi

say "Changes since ${CURRENT_COMMIT:-the deployed version}"
if [[ -n "$CURRENT_COMMIT" ]] && g cat-file -e "$CURRENT_COMMIT^{commit}" 2>/dev/null; then
  LOG="$(g log --oneline --no-decorate "$CURRENT_COMMIT..$TARGET_COMMIT" 2>/dev/null | head -25)"
  if [[ -n "$LOG" ]]; then
    sed 's/^/      /' <<<"$LOG"
  else
    echo "      (none — $TARGET is not ahead of the deployed commit; this moves to a different or older build)"
  fi
else
  g log --oneline --no-decorate -10 "$TARGET_COMMIT" | sed 's/^/      /'
fi
echo

if (( CHECK_ONLY )); then
  # --check is a plan, and a plan is never an error: it reports what a real
  # run would need instead of failing the way a real run would.
  if (( IS_MAJOR && ! ALLOW_MAJOR )); then
    echo "  MAJOR version bump ($CUR_MAJOR -> $NEW_MAJOR): a real update needs --allow-major."
    echo "  Read the notes first:  https://github.com/Seth-Smithey/homelab-panel/releases/tag/$TARGET"
    echo
  fi
  if (( IS_DOWNGRADE && ! ALLOW_DOWNGRADE )); then
    echo "  $TARGET is OLDER than the deployed build: a real update needs --allow-downgrade"
    echo "  (or use --rollback, which also restores the matching database)."
    echo
  fi
  say "--check: nothing was changed (fetched refs only)."
  exit 0
fi

if (( IS_DOWNGRADE && ! ALLOW_DOWNGRADE )); then
  echo "  $TARGET is OLDER than the deployed build ($CURRENT_COMMIT)."
  echo "  To go back to a previous release with its database:  sudo panelctl update --rollback"
  echo "  To install this older build anyway:                   re-run with --allow-downgrade"
  exit 3
fi

if (( IS_MAJOR && ! ALLOW_MAJOR )); then
  echo "  This is a MAJOR version bump ($CUR_MAJOR -> $NEW_MAJOR) — the only kind that can need a manual step."
  echo "  Read the notes:  https://github.com/Seth-Smithey/homelab-panel/releases/tag/$TARGET"
  echo "  Then re-run with --allow-major."
  exit 3
fi

# Disk: the new release needs room for a venv, and the backup for a DB copy.
NEED_MB=300
DB_MB="$(du -m "$APP_DIR/data/panel.db" 2>/dev/null | cut -f1 || echo 0)"
AVAIL_OPT="$(df -Pm /opt | awk 'NR==2 {print $4}')"
AVAIL_BAK="$(df -Pm "$BACKUP_ROOT" | awk 'NR==2 {print $4}')"
(( AVAIL_OPT > NEED_MB )) || die "Only ${AVAIL_OPT} MB free on /opt; a new release needs about ${NEED_MB} MB."
(( AVAIL_BAK > DB_MB + 20 )) || die "Only ${AVAIL_BAK} MB free for backups; the database alone is ${DB_MB} MB."

# ---------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------

say "Checking out $TARGET"
PREV_HEAD="$(g rev-parse HEAD)"
g checkout --quiet --force "$TARGET_COMMIT" || die "Checkout failed. Nothing running was changed."

# install.sh takes the backup itself, immediately before activation (so the
# least possible data is discarded on revert), and restores it on failure.
# It reports the backup path through this file.
BACKUP_OUT="$PANEL_UPDATE_RUNNER/backup-path"
say "Installing (builds and validates the new release before switching)"
if PANEL_NO_SYSTEMD="$NO_SYSTEMD" PANEL_LOCK_HELD=1 PANEL_BACKUP_OUT="$BACKUP_OUT" bash "$SRC_DIR/install.sh"; then
  BACKUP="$(cat "$BACKUP_OUT" 2>/dev/null || true)"
else
  BACKUP="$(cat "$BACKUP_OUT" 2>/dev/null || true)"
  warn "install.sh failed. If it got as far as switching, it has reverted: previous release, database and config restored."
  g checkout --quiet --force "$PREV_HEAD" 2>/dev/null || true
  echo
  echo "  Deployed release: $(jget "$APP_DIR/manifest.json" version) — verify with: sudo $APP_DIR/panelctl status" >&2
  [[ -n "$BACKUP" ]] && echo "  Backup kept at:   $BACKUP" >&2
  exit 1
fi

prune_backups

# Only worth showing when THIS release changed the example; otherwise a
# starter-config install would be told about every optional collector after
# every update.
PREV_DIR="$(jget "$APP_DIR/manifest.json" previous_release_dir)"
DIFF_OUT=""
if [[ -z "$PREV_DIR" || ! -f "$PREV_DIR/config.example.yaml" ]] \
   || ! cmp -s "$PREV_DIR/config.example.yaml" "$APP_DIR/current/config.example.yaml"; then
  DIFF_OUT="$(ctl config-diff 2>/dev/null || true)"
fi
if grep -q '^  +' <<<"$DIFF_OUT"; then
  echo
  say "This release adds config options you do not have"
  sed 's/^/    /' <<<"$DIFF_OUT"
  echo
  echo "    Your config.yaml was not modified. Every new key has a default."
fi

cat <<EOF

Updated ${CURRENT_VERSION:-?} -> $NEW_VERSION

  Backup            ${BACKUP:-none needed}
  Undo this         sudo panelctl update --rollback
  What's deployed   sudo panelctl status
  Follow logs       sudo panelctl logs

EOF
