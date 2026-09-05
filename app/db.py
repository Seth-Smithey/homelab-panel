"""SQLite persistence: metric history for sparklines, plus a state-change log.

Deliberately small. The panel is a live view; history exists to answer
"has this been flapping?" not to replace Splunk.

The schema lives in app/migrations.py, not here, so a database written by an
older version can be walked forward on update rather than needing a wipe.
"""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .migrations import current_version, migrate

# Hard ceilings on anything a caller can ask for. Without these, `?points=`
# or a negative `limit` becomes an unbounded read of a multi-million-row
# table, held under the connection lock while every collector waits.
MAX_HISTORY_POINTS = 5000
MAX_EVENTS = 500
MAX_SPARKLINE_IDS = 60

# The longest a mute may last. Anything non-finite or absurd is clamped:
# an `inf` here reaches SQLite, then json.dumps(allow_nan=False), and every
# subsequent API response 500s permanently — including after a restart,
# because the row persists and never expires.
MAX_MUTE_HOURS = 24 * 365


def _clamp_int(value: Any, default: int, ceiling: int, floor: int = 1) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(floor, min(number, ceiling))


class StoreClosed(RuntimeError):
    """Raised instead of sqlite3.ProgrammingError when a late task touches a
    store that shutdown already closed."""


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._closed = False
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        # Durability we can afford to trade: WAL + NORMAL means a power cut
        # can cost the last few seconds of metric samples, which are samples
        # of a live gauge we re-read seconds later anyway. In exchange every
        # commit stops being an fsync, and commits happen on the event loop.
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self.schema_from, self.schema_to = migrate(self._conn)

    # -- lifecycle -----------------------------------------------------

    @property
    def schema_version(self) -> int:
        with self._lock:
            if self._closed:
                return self.schema_to
            return current_version(self._conn)

    def _guard(self) -> None:
        if self._closed:
            raise StoreClosed("the database was closed during shutdown")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                # Fold the WAL back into the main file so a backup taken by
                # update.sh (a plain file copy) is complete on its own.
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._conn.close()

    # -- writes --------------------------------------------------------

    def record_observation(self, panel: str, ts: float, interval: float) -> None:
        """A collector completed a successful poll at `ts`."""
        with self._lock:
            self._guard()
            self._conn.execute(
                "INSERT INTO observations (panel, ts, interval) VALUES (?, ?, ?)",
                (panel, float(ts), max(1.0, float(interval))),
            )
            self._conn.commit()

    def observed_spans(self, panel: str, start: float, end: float) -> list[tuple[float, float]]:
        """Intervals within [start, end] during which `panel` was being polled.

        Two consecutive successful polls closer than three intervals apart
        bound an observed span; a larger gap is time nobody was looking. The
        last poll vouches for up to three intervals after itself.
        """
        with self._lock:
            self._guard()
            rows = self._conn.execute(
                "SELECT ts, interval FROM observations WHERE panel = ? AND ts >= ? AND ts <= ?"
                " ORDER BY ts ASC",
                (panel, start - 86400, end),
            ).fetchall()
        spans: list[tuple[float, float]] = []
        for i, row in enumerate(rows):
            ts, reach = float(row["ts"]), 3 * float(row["interval"])
            nxt = float(rows[i + 1]["ts"]) if i + 1 < len(rows) else end
            span_end = min(nxt, ts + reach, end)
            a, b = max(ts, start), span_end
            if b <= a:
                continue
            if spans and a <= spans[-1][1]:
                spans[-1] = (spans[-1][0], max(spans[-1][1], b))
            else:
                spans.append((a, b))
        return spans

    def record_metrics(self, rows: list[tuple[str, float]]) -> None:
        if not rows:
            return
        now = time.time()
        clean = [
            (cid, now, float(val))
            for cid, val in rows
            # NaN/inf would poison both the sparkline maths and json.dumps.
            if isinstance(val, (int, float)) and math.isfinite(val)
        ]
        if not clean:
            return
        with self._lock:
            self._guard()
            self._conn.executemany(
                "INSERT INTO metrics (check_id, ts, value) VALUES (?, ?, ?)", clean
            )
            self._conn.commit()

    def record_event(
        self, check_id: str, name: str, panel: str, old: str, new: str, value: str = ""
    ) -> None:
        with self._lock:
            self._guard()
            self._conn.execute(
                "INSERT INTO events (ts, check_id, name, panel, old, new, value)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), check_id, name, panel, old, new, value),
            )
            self._conn.commit()

    # -- severity cache (survives restarts) ----------------------------

    def load_severities(self) -> dict[str, dict[str, Any]]:
        """Last known state per check: {id: {severity, notified, first_seen}}.

        Read once at startup. Without it, every check looks new after a
        restart, so a failure that started while the service was down never
        produces a transition and never alerts.
        """
        with self._lock:
            self._guard()
            rows = self._conn.execute(
                "SELECT check_id, severity, notified, first_seen, panel, name, ephemeral"
                " FROM check_severity"
            ).fetchall()
        # `notified` NULL means "never delivered" and must stay None — mapping
        # it to the severity on load would mark an undelivered first-seen
        # failure as already notified after a restart, losing the alert for
        # good. (Legacy rows were backfilled by migration 003, so NULL here is
        # always a genuine not-yet.)
        return {
            r["check_id"]: {
                "severity": r["severity"],
                "notified": r["notified"],
                "first_seen": r["first_seen"],
                "panel": r["panel"],
                "name": r["name"] or "",
                "ephemeral": bool(r["ephemeral"]),
            }
            for r in rows
        }

    def save_severities(self, rows: list[tuple]) -> None:
        """Persist (check_id, severity, name, panel, notified[, ephemeral]) per check.

        `first_seen` is set on insert only, never on update — it is the
        observation start that bounds availability. `notified` None means
        "leave what is stored".
        """
        if not rows:
            return
        now = time.time()
        params = []
        for row in rows:
            cid, sev, name, panel, notified = row[:5]
            ephemeral = 1 if (len(row) > 5 and row[5]) else 0
            params.append((cid, sev, name, panel, now, now, notified, ephemeral))
        with self._lock:
            self._guard()
            self._conn.executemany(
                "INSERT INTO check_severity"
                " (check_id, severity, name, panel, updated, first_seen, notified, ephemeral)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(check_id) DO UPDATE SET severity=excluded.severity,"
                " name=excluded.name, panel=excluded.panel, updated=excluded.updated,"
                " notified=COALESCE(excluded.notified, check_severity.notified),"
                " ephemeral=excluded.ephemeral",
                params,
            )
            self._conn.commit()

    def first_seen(self, check_id: str) -> float | None:
        with self._lock:
            self._guard()
            row = self._conn.execute(
                "SELECT first_seen FROM check_severity WHERE check_id = ?", (check_id,)
            ).fetchone()
        return float(row["first_seen"]) if row and row["first_seen"] is not None else None

    def forget_severities(self, check_ids: list[str]) -> None:
        if not check_ids:
            return
        with self._lock:
            self._guard()
            self._conn.executemany(
                "DELETE FROM check_severity WHERE check_id = ?",
                [(cid,) for cid in check_ids],
            )
            self._conn.commit()

    # -- reads ---------------------------------------------------------

    def history(self, check_id: str, limit: int = 120) -> list[dict[str, Any]]:
        limit = _clamp_int(limit, 120, MAX_HISTORY_POINTS)
        with self._lock:
            self._guard()
            rows = self._conn.execute(
                "SELECT ts, value FROM metrics WHERE check_id = ?"
                " ORDER BY ts DESC LIMIT ?",
                (check_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def sparklines(self, check_ids: list[str], points: int = 40) -> dict[str, list[float]]:
        """Recent values for several checks.

        One lock acquisition, N bounded index reads. Each per-id query walks
        `idx_metrics_check_ts` and stops after `points` rows, so the cost is
        proportional to what is returned — unlike a window-function rank over
        every retained row for those ids, which was the previous attempt and
        is not automatically cheaper just because it is one statement. The
        original N+1 problem was the N lock acquisitions, not the N queries.
        """
        ids = [c for c in check_ids if c][:MAX_SPARKLINE_IDS]
        if not ids:
            return {}
        points = _clamp_int(points, 40, MAX_HISTORY_POINTS)
        out: dict[str, list[float]] = {}
        with self._lock:
            self._guard()
            for cid in ids:
                rows = self._conn.execute(
                    "SELECT value FROM metrics WHERE check_id = ?"
                    " ORDER BY ts DESC LIMIT ?",
                    (cid, points),
                ).fetchall()
                if rows:
                    out[cid] = [r["value"] for r in reversed(rows)]
        return out

    def events(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = _clamp_int(limit, 50, MAX_EVENTS)
        with self._lock:
            self._guard()
            rows = self._conn.execute(
                "SELECT ts, check_id, name, panel, old, new, value FROM events"
                " ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- mutes ---------------------------------------------------------

    def set_mute(self, check_id: str, reason: str = "", hours: float | None = None) -> dict:
        """Silence a check. hours=None mutes it until someone unmutes it."""
        until = None
        if hours is not None:
            try:
                span = float(hours)
            except (TypeError, ValueError):
                span = 0.0
            if not math.isfinite(span) or span <= 0:
                raise ValueError("hours must be a positive, finite number")
            span = min(span, MAX_MUTE_HOURS)
            until = time.time() + span * 3600
        with self._lock:
            self._guard()
            self._conn.execute(
                "INSERT INTO mutes (check_id, reason, until, created) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(check_id) DO UPDATE SET reason=excluded.reason,"
                " until=excluded.until, created=excluded.created",
                (check_id, str(reason)[:200], until, time.time()),
            )
            self._conn.commit()
        return {"check_id": check_id, "reason": reason, "until": until}

    def clear_mute(self, check_id: str) -> bool:
        with self._lock:
            self._guard()
            cur = self._conn.execute("DELETE FROM mutes WHERE check_id = ?", (check_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def active_mutes(self) -> dict[str, dict[str, Any]]:
        """Current mutes. A pure read.

        This runs on every snapshot — which means every collector poll and
        every SSE broadcast, dozens of times a minute. It used to DELETE
        expired rows and commit on each of those calls. Expiry now happens
        in the maintenance loop (see expire_mutes); filtering here keeps the
        result correct in between.
        """
        now = time.time()
        with self._lock:
            self._guard()
            rows = self._conn.execute(
                "SELECT check_id, reason, until, created FROM mutes"
                " WHERE until IS NULL OR until >= ?",
                (now,),
            ).fetchall()
        return {r["check_id"]: dict(r) for r in rows}

    def expire_mutes(self) -> int:
        with self._lock:
            self._guard()
            cur = self._conn.execute(
                "DELETE FROM mutes WHERE until IS NOT NULL AND until < ?", (time.time(),)
            )
            self._conn.commit()
            return cur.rowcount

    # -- availability --------------------------------------------------

    def availability(self, check_id: str, hours: float, current: str) -> dict[str, Any]:
        """Percent of the *observed* window spent in each severity.

        Reconstructed from the transition log: the state entering the window
        is whatever the last transition before it set, and each event ends the
        preceding span. Coverage starts at the later of the window start and
        the check's `first_seen` — without that, a check discovered an hour
        ago acquired an apparent 24-hour history, and the 23 unobserved hours
        were silently counted as whatever state it happened to be in first.
        The response says how much of the requested window was actually
        observed, so 100% healthy over 4% coverage reads as what it is.
        """
        now = time.time()
        try:
            span_hours = float(hours)
        except (TypeError, ValueError):
            span_hours = 24.0
        if not math.isfinite(span_hours):
            span_hours = 24.0
        span_hours = max(0.1, min(span_hours, 24 * 365))
        requested_start = now - span_hours * 3600

        with self._lock:
            self._guard()
            seen_row = self._conn.execute(
                "SELECT first_seen, panel FROM check_severity WHERE check_id = ?", (check_id,)
            ).fetchone()
            first_seen = (
                float(seen_row["first_seen"])
                if seen_row and seen_row["first_seen"] is not None
                else None
            )
            panel_key = str(seen_row["panel"]) if seen_row and seen_row["panel"] else ""
            start = max(requested_start, first_seen) if first_seen else requested_start
            prior = self._conn.execute(
                "SELECT new FROM events WHERE check_id = ? AND ts < ?"
                " ORDER BY ts DESC LIMIT 1",
                (check_id, start),
            ).fetchone()
            rows = self._conn.execute(
                "SELECT ts, old, new FROM events WHERE check_id = ? AND ts >= ?"
                " ORDER BY ts ASC",
                (check_id, start),
            ).fetchall()

        # "new" is the synthetic previous state of a first-seen check, not a
        # severity anything spent time in. Treat it as unobserved.
        def normalise(state: str) -> str:
            return "unobserved" if state in ("new", "gone") else state

        if prior is not None:
            state = normalise(prior["new"])
        elif rows and first_seen is not None:
            state = normalise(rows[0]["old"])
        elif first_seen is None:
            # No first_seen row means observation is unbounded — either never
            # seen, or forgotten after it resolved. Time before the first event
            # (or the whole window) cannot be vouched for.
            state = "unobserved"
        else:
            state = current

        # The state timeline: [(from, to, state), ...] across the window.
        timeline: list[tuple[float, float, str]] = []
        cursor = start
        for row in rows:
            timeline.append((cursor, max(cursor, float(row["ts"])), state))
            cursor = float(row["ts"])
            state = normalise(row["new"])
        timeline.append((cursor, max(cursor, now), state))

        # Only time the collector was actually looking counts. Where no
        # observation log exists yet (a database from before it was kept),
        # the whole bounded window is taken as observed, as before.
        looked = self.observed_spans(panel_key, start, now) if panel_key else []
        has_log = bool(looked) or self._has_observations(panel_key)
        if not has_log:
            looked = [(start, now)]

        spans: dict[str, float] = {}
        for a, b, st in timeline:
            for la, lb in looked:
                overlap = min(b, lb) - max(a, la)
                if overlap > 0:
                    spans[st] = spans.get(st, 0.0) + overlap

        requested = max(now - requested_start, 1.0)
        spans.pop("unobserved", None)
        observed_measured = max(sum(spans.values()), 0.0)
        unobserved_seconds = max(requested - observed_measured, 0.0)
        denominator = max(observed_measured, 1.0)
        percents = {k: round(v / denominator * 100, 3) for k, v in spans.items()}
        return {
            "check_id": check_id,
            "hours": span_hours,
            "percent": percents,
            "ok_percent": percents.get("ok", 0.0),
            "coverage_percent": round(observed_measured / requested * 100, 1),
            "observed_seconds": round(observed_measured, 1),
            "unobserved_seconds": round(unobserved_seconds, 1),
            "first_seen": first_seen,
            "transitions": len(rows),
            # Observed means some measured time exists — not merely that a row
            # or event exists. A check whose only event is "gone" has nothing.
            "observed": observed_measured > 0,
            # "polled": coverage comes from the collector's own poll log.
            # "assumed": no poll log for this panel yet; bounded by first_seen.
            "coverage_basis": "polled" if has_log else "assumed",
        }

    def _has_observations(self, panel: str) -> bool:
        if not panel:
            return False
        with self._lock:
            self._guard()
            row = self._conn.execute(
                "SELECT 1 FROM observations WHERE panel = ? LIMIT 1", (panel,)
            ).fetchone()
        return row is not None

    # -- maintenance ---------------------------------------------------

    def prune(self, retention_days: int = 14) -> int:
        """Metrics expire on schedule; events live twice as long because
        availability is reconstructed from them."""
        try:
            days = int(retention_days)
        except (TypeError, ValueError):
            days = 14
        days = max(1, min(days, 3650))
        now = time.time()
        with self._lock:
            self._guard()
            cur = self._conn.execute(
                "DELETE FROM metrics WHERE ts < ?", (now - days * 86400,)
            )
            self._conn.execute(
                "DELETE FROM events WHERE ts < ?", (now - days * 172800,)
            )
            self._conn.execute(
                "DELETE FROM observations WHERE ts < ?", (now - days * 172800,)
            )
            # check_severity is NOT pruned here: one row per check, and the
            # engine forgets rows explicitly when a check disappears. Age-based
            # pruning removed rows for checks whose collector had merely been
            # erroring for a while, resetting first_seen and notified on recovery.
            self._conn.commit()
            return cur.rowcount

    def vacuum(self) -> None:
        """Return freed pages to the filesystem after a large prune."""
        with self._lock:
            self._guard()
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.execute("VACUUM")

    def stats(self) -> dict[str, Any]:
        with self._lock:
            self._guard()
            metrics = self._conn.execute("SELECT COUNT(*) AS n FROM metrics").fetchone()["n"]
            events = self._conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
            mutes = self._conn.execute("SELECT COUNT(*) AS n FROM mutes").fetchone()["n"]
        size = self.path.stat().st_size if self.path.exists() else 0
        return {
            "metrics_rows": metrics,
            "events_rows": events,
            "mutes": mutes,
            "bytes": size,
            "schema_version": self.schema_to,
        }
