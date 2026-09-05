#!/usr/bin/env bash
#
# Real install → update → failed schema-changing update → rollback, with the
# actual install.sh and update.sh (PANEL_NO_SYSTEMD=1: unit management is a
# no-op and the release is started directly for the health check). Everything
# else is the real code path. CI runs this; so can you, in a throwaway VM or
# container, as root:
#
#   sudo tests/smoke/install-update-rollback.sh
#
# It builds a fake git origin with four releases from the working tree:
#
#   v<current>        what is checked out
#   v98.0.0           schema 4 + never becomes ready  (must fail AND revert)
#   v99.0.0           a good newer stable release
#   v100.0.0-test     a decoy pre-release the stable channel must skip
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="${SMOKE_WORK:-/tmp/panel-smoke}"
export PANEL_NO_SYSTEMD=1 PANELCTL_NO_SYSTEMD=1

step() { echo; echo "##### $*"; }
fail() { echo "SMOKE FAILED: $*" >&2; exit 1; }
manifest_version() { python3 -c 'import json;print(json.load(open("/opt/homelab-panel/manifest.json"))["version"])'; }
db_schema() { python3 -c 'import sqlite3;print(sqlite3.connect("file:/opt/homelab-panel/data/panel.db?mode=ro", uri=True).execute("PRAGMA user_version").fetchone()[0])'; }
sha_tree() { find "$1" -type f -not -name '*.pyc' -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16; }

[[ $EUID -eq 0 ]] || fail "run as root"
rm -rf "$WORK" /opt/homelab-panel /var/backups/homelab-panel /usr/local/bin/panelctl
mkdir -p "$WORK"

step "Build a fake origin with four releases"
git config --global user.email ci@example.com && git config --global user.name ci
git config --global --add safe.directory "$ROOT"   # root reading the runner's checkout
git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1 || fail "$ROOT is not a git checkout"
git clone -q --bare "$ROOT" "$WORK/origin.git"
git clone -q "$WORK/origin.git" "$WORK/clone"
cd "$WORK/clone"
CUR="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' app/version.py)"
SCHEMA="$(sed -n 's/^SCHEMA_VERSION = \([0-9]*\).*/\1/p' app/version.py)"
git tag -a "v$CUR" -m "$CUR" 2>/dev/null || true

# v98: a schema migration (current + 1) plus a release that never reports ready.
sed -i "s/__version__ = \"$CUR\"/__version__ = \"98.0.0\"/" app/version.py
sed -i "s/^SCHEMA_VERSION = .*/SCHEMA_VERSION = $((SCHEMA + 1))/" app/version.py
python3 - <<'PY'
import re
p = "app/migrations.py"
s = open(p).read()
s = s.replace(
    "# Index i applies migration i+1. Append only.",
    "def _m_smoke(conn):\n"
    "    conn.execute(\"CREATE TABLE IF NOT EXISTS smoke_only (id INTEGER PRIMARY KEY)\")\n\n\n"
    "# Index i applies migration i+1. Append only.",
)
s2 = re.sub(r"(MIGRATIONS: list\[Callable\[\[sqlite3\.Connection\], None\]\] = \[\n(?:    _m\w+,\n)+)\]",
            r"\1    _m_smoke,\n]", s, count=1)
assert s2 != s, "could not append the smoke migration"
open(p, "w").write(s2)
p = "app/main.py"
s = open(p).read()
s2 = re.sub(r'"ready": bool\(eng\) and', '"ready": False and', s, count=1)
assert s2 != s, "could not sabotage readiness"
open(p, "w").write(s2)
PY
git commit -qam "98.0.0: schema 4, never ready" && git tag -a v98.0.0 -m v98

# v99: a good release on top of the current code (not on top of v98).
git checkout -q "v$CUR"
sed -i "s/__version__ = \"$CUR\"/__version__ = \"99.0.0\"/" app/version.py
git commit -qam "99.0.0" && git tag -a v99.0.0 -m v99
sed -i 's/__version__ = "99.0.0"/__version__ = "100.0.0-test"/' app/version.py
git commit -qam decoy && git tag -a v100.0.0-test -m decoy
git push -q origin --tags
git checkout -q "v$CUR"

step "Install $CUR"
./install.sh
[[ "$(manifest_version)" == "$CUR" ]] || fail "manifest should say $CUR"
[[ "$(db_schema)" == "$SCHEMA" ]] || fail "schema should be $SCHEMA"
( cd /tmp && panelctl check ) || fail "panelctl check"
panelctl status
CURRENT_REL="$(readlink -f /opt/homelab-panel/current)"
BEFORE="$(sha_tree "$CURRENT_REL")"

step "Reinstalling the same commit must not touch the current release"
./install.sh 2>&1 | tee "$WORK/reinstall.txt"
grep -q "files untouched\|Nothing to do" "$WORK/reinstall.txt" || fail "reinstall should reuse or skip"
[[ "$(sha_tree "$CURRENT_REL")" == "$BEFORE" ]] || fail "current release was modified by a reinstall"
[[ "$(readlink -f /opt/homelab-panel/current)" == "$CURRENT_REL" ]] || fail "current changed on reinstall"

step "Plan with --check (major bump must be reported, not fatal)"
./update.sh --check | tee "$WORK/check.txt"
grep -q "Available   99.0.0" "$WORK/check.txt" || fail "--check should offer 99.0.0"
grep -q "allow-major" "$WORK/check.txt" || fail "--check should mention --allow-major"
! grep -q "100.0.0-test" "$WORK/check.txt" || fail "--check must skip the pre-release decoy"

step "A real update must refuse an unapproved major bump"
if ./update.sh; then fail "should have refused the major bump"; fi
[[ "$(manifest_version)" == "$CUR" ]] || fail "refusal must change nothing"

step "Seed the database with state the rollback must preserve"
python3 - <<'PY'
import sqlite3, time
c = sqlite3.connect("/opt/homelab-panel/data/panel.db")
c.execute("INSERT OR REPLACE INTO mutes (check_id, reason, until, created) VALUES ('smoke.check', 'smoke test', NULL, ?)", (time.time(),))
c.commit(); c.close()
PY
MUTES_BEFORE="$(python3 -c 'import sqlite3;print(sqlite3.connect("/opt/homelab-panel/data/panel.db").execute("select count(*) from mutes").fetchone()[0])')"
cp -p /opt/homelab-panel/panelctl "$WORK/panelctl.before"

step "Schema-changing update to a release that never becomes ready: must fail and revert"
if ./update.sh --to v98.0.0 --allow-major 2>&1 | tee "$WORK/bad.txt"; then fail "the v98 update should have failed"; fi
grep -q "Restored database" "$WORK/bad.txt" || fail "revert should report restoring the database"
grep -q "smoke_only\|migrating database schema" "$WORK/bad.txt" || true
[[ "$(manifest_version)" == "$CUR" ]] || fail "manifest should still say $CUR after the failed update"
[[ "$(db_schema)" == "$SCHEMA" ]] || fail "database schema must be back at $SCHEMA (got $(db_schema))"
[[ "$(readlink -f /opt/homelab-panel/current)" == "$CURRENT_REL" ]] || fail "current should point at the old release"
cmp -s /opt/homelab-panel/panelctl "$WORK/panelctl.before" || fail "panelctl was not restored"
MUTES_AFTER="$(python3 -c 'import sqlite3;print(sqlite3.connect("/opt/homelab-panel/data/panel.db").execute("select count(*) from mutes").fetchone()[0])')"
[[ "$MUTES_BEFORE" == "$MUTES_AFTER" ]] || fail "mutes were lost in the revert"
# The old release must still start against the restored database.
python3 -c 'import sqlite3;c=sqlite3.connect("/opt/homelab-panel/data/panel.db");assert not c.execute("select name from sqlite_master where name=\"smoke_only\"").fetchall(), "schema-4 table survived the revert"'
( cd /tmp && panelctl check ) || fail "old release rejects the restored config"

step "Update to the newer stable release"
./update.sh --allow-major
[[ "$(manifest_version)" == "99.0.0" ]] || fail "manifest should say 99.0.0"
./update.sh --list-backups | tee "$WORK/backups.txt"
grep -q "complete, verified" "$WORK/backups.txt" || fail "expected a verified backup"

step "A damaged backup must be refused before anything changes"
LATEST="$(ls -1dt /var/backups/homelab-panel/*/ | head -1)"
cp -a "$LATEST" "$WORK/damaged"
echo "tamper" >> "$WORK/damaged/config.yaml"
mv "$WORK/damaged" "/var/backups/homelab-panel/damaged-smoke"
if ./update.sh --rollback damaged-smoke; then fail "rollback accepted a damaged backup"; fi
[[ "$(manifest_version)" == "99.0.0" ]] || fail "a refused rollback must change nothing"
rm -rf /var/backups/homelab-panel/damaged-smoke

step "Roll back"
./update.sh --rollback
[[ "$(manifest_version)" == "$CUR" ]] || fail "rollback should restore $CUR"
[[ "$(db_schema)" == "$SCHEMA" ]] || fail "rollback should restore schema $SCHEMA"

step "panelctl update --check from anywhere"
( cd / && panelctl update --check ) > "$WORK/pc-check.txt" 2>&1 || fail "panelctl update --check exited non-zero"
grep -q "Available   99.0.0" "$WORK/pc-check.txt" || fail "panelctl update --check did not report 99.0.0"

echo
echo "install → reinstall → failed schema update (reverted) → update → refused damaged rollback → rollback: all passed"
