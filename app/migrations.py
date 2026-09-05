"""Forward-only SQLite migrations.

The rules, because an operator on a homelab box updates by pulling and
restarting and nobody is watching the logs:

1. **Forward only.** There is no downgrade path. A rollback restores the
   backup `update.sh` took before migrating, which is a file copy and always
   works — unlike a down-migration, which has to guess what the data meant.
2. **Never edit a released migration.** Add a new one. A database out there
   has already run the old one and will not run it again.
3. **Additive first.** Adding a table or a column is safe against both the
   old and new code (expand/contract): the new binary can start before the
   contract step ever happens, and a half-finished update leaves a database
   the previous version can still read.
4. **Idempotent.** Every statement is IF NOT EXISTS or guarded, so a crash
   between the statement and the version bump is recoverable by re-running.

Version is tracked in SQLite's own `PRAGMA user_version`, so there is no
bootstrap problem — a brand new file reports 0 without needing a table to
exist first.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable

log = logging.getLogger("panel.migrations")


def _run(conn: sqlite3.Connection, script: str) -> None:
    """Execute a multi-statement script inside the *caller's* transaction.

    `executescript()` commits any open transaction before it runs, so a
    migration body and its `PRAGMA user_version` bump were never atomic — a
    crash between them left the schema changed and the version not, and the
    next start re-ran the migration. Splitting on ';' and using execute()
    keeps everything inside the one transaction `migrate()` opens.
    """
    for statement in script.split(";"):
        statement = statement.strip()
        if statement:
            conn.execute(statement)


def _m001_initial(conn: sqlite3.Connection) -> None:
    """The original schema: metrics, events, mutes."""
    _run(
        conn,
        """
        CREATE TABLE IF NOT EXISTS metrics (
            check_id TEXT NOT NULL,
            ts       REAL NOT NULL,
            value    REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_metrics_check_ts ON metrics(check_id, ts DESC);

        CREATE TABLE IF NOT EXISTS events (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts        REAL NOT NULL,
            check_id  TEXT NOT NULL,
            name      TEXT NOT NULL,
            panel     TEXT NOT NULL,
            old       TEXT NOT NULL,
            new       TEXT NOT NULL,
            value     TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);

        CREATE TABLE IF NOT EXISTS mutes (
            check_id TEXT PRIMARY KEY,
            reason   TEXT NOT NULL DEFAULT '',
            until    REAL,
            created  REAL NOT NULL
        );
        """
    )


def _m002_indexes_and_severity(conn: sqlite3.Connection) -> None:
    """Indexes the queries actually need, plus persisted severity.

    Two problems this fixes:

    `prune` deletes on `ts`, but the only metrics index led with `check_id`,
    so every hourly prune was a full table scan of what reaches millions of
    rows — blocking the event loop, and therefore every collector and every
    HTTP request, for its duration. Same story for `availability`, which
    filters on `check_id` against an index led by `ts`.

    `check_severity` persists the last severity seen per check. It was
    in-memory only, so after any restart every check looked new, no
    transition was recorded, and a failure that began during the restart
    never alerted — permanently, not just late.
    """
    _run(
        conn,
        """
        CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts);
        CREATE INDEX IF NOT EXISTS idx_events_check_ts ON events(check_id, ts);

        CREATE TABLE IF NOT EXISTS check_severity (
            check_id TEXT PRIMARY KEY,
            severity TEXT NOT NULL,
            name     TEXT NOT NULL DEFAULT '',
            panel    TEXT NOT NULL DEFAULT '',
            updated  REAL NOT NULL
        );
        """
    )


def _m003_observation_coverage(conn: sqlite3.Connection) -> None:
    """When did we first see each check, and what has been notified?

    `first_seen` bounds availability: without it a check discovered an hour
    ago acquired an apparent 24-hour history, with the unobserved 23 hours
    counted as whatever state it happened to be in first. `notified` is the
    severity that was last *delivered*, separately from what was last
    *observed* — the startup grace period suppressed the notification while
    still recording the state, so a failure that began during a restart was
    never reported once grace ended.

    ALTER TABLE ADD COLUMN is not idempotent in SQLite, so each is guarded
    by a look at the existing columns.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(check_severity)")}
    if "first_seen" not in existing:
        conn.execute("ALTER TABLE check_severity ADD COLUMN first_seen REAL")
        conn.execute("UPDATE check_severity SET first_seen = updated WHERE first_seen IS NULL")
    if "notified" not in existing:
        conn.execute("ALTER TABLE check_severity ADD COLUMN notified TEXT")
        conn.execute("UPDATE check_severity SET notified = severity WHERE notified IS NULL")


def _m004_ephemeral_and_observations(conn: sqlite3.Connection) -> None:
    """Event-like checks, and when each collector actually looked.

    `ephemeral`: a failed task or a noisy rule ages out of its window rather
    than recovering. Without remembering that across a restart, a failed
    backup from yesterday that fell out of the 24h window while the panel
    was down was announced as "recovered" on the first poll after startup.

    `observations`: one row per successful poll per collector. Availability
    used to count every second between a check's first sighting and now as
    observed, so a 12-hour outage of the panel itself (or of the API it
    reads) was reported as 12 hours of whatever state came before it. Time
    between two polls more than three intervals apart is now unobserved.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(check_severity)")}
    if "ephemeral" not in existing:
        conn.execute("ALTER TABLE check_severity ADD COLUMN ephemeral INTEGER NOT NULL DEFAULT 0")
    _run(
        conn,
        """
        CREATE TABLE IF NOT EXISTS observations (
            panel    TEXT NOT NULL,
            ts       REAL NOT NULL,
            interval REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_observations_panel_ts ON observations(panel, ts);
        """
    )


def _m005_sessions(conn: sqlite3.Connection) -> None:
    """Server-side browser sessions.

    The session cookie used to carry the API token itself, so every browser
    that had ever logged in held the credential in clear text for 30 days.
    Now the cookie is a random id and only its SHA-256 is stored here, with
    a fingerprint of the token it was issued under so rotating the token
    invalidates every session at once. Persisted so a restart does not log
    every wall display out.
    """
    _run(
        conn,
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id_hash  TEXT PRIMARY KEY,
            token_fp TEXT NOT NULL,
            created  REAL NOT NULL,
            expires  REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires);
        """
    )


# Index i applies migration i+1. Append only.
MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _m001_initial,
    _m002_indexes_and_severity,
    _m003_observation_coverage,
    _m004_ephemeral_and_observations,
    _m005_sessions,
]


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def target_version() -> int:
    return len(MIGRATIONS)


def migrate(conn: sqlite3.Connection) -> tuple[int, int]:
    """Walk the database forward. Returns (from_version, to_version).

    Raises if the database is NEWER than this code understands: that means
    someone downgraded the application, and letting an old binary write to a
    schema it does not know is how data gets silently corrupted. Restoring
    the pre-update backup is the correct recovery, and update.sh takes one.
    """
    start = current_version(conn)
    target = target_version()

    if start > target:
        raise RuntimeError(
            f"The database is at schema v{start} but this build only knows v{target}. "
            "It was written by a newer version of the panel. Restore the backup "
            "update.sh took, or reinstall the newer version — do not run this one "
            "against it."
        )

    if start == target:
        return start, target

    log.info("migrating database schema v%d -> v%d", start, target)
    for index in range(start, target):
        step = MIGRATIONS[index]
        log.info("  applying migration %03d (%s)", index + 1, step.__name__)
        # The body and the version bump are one transaction. A crash in the
        # middle rolls both back; the next start re-runs the step from clean.
        # `isolation_level=None` puts the connection in autocommit so BEGIN
        # below is honoured literally rather than being managed implicitly.
        previous_isolation = conn.isolation_level
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                step(conn)
                conn.execute(f"PRAGMA user_version = {index + 1}")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.isolation_level = previous_isolation

    log.info("schema is now v%d", target)
    return start, target
