"""Snapshot honesty, non-finite values, staleness, the store, migrations."""

from __future__ import annotations

import json
import math
import sqlite3
import tempfile
import time

import pytest

from app.db import MAX_MUTE_HOURS, Store, StoreClosed
from app.migrations import MIGRATIONS, current_version, migrate
from app.models import OK, UNKNOWN, WARNING, Check, Panel
from app.scheduler import Engine
from app.version import SCHEMA_VERSION

from .conftest import check, make_panel, write_config

WITH_HOST = (
    "config_version: 1\nsite: {name: t}\nserver: {host: 127.0.0.1, port: 8080, api_token: ''}\n"
    "poll: {default_interval: 5}\nalerts: {enabled: false}\n"
    # Registered so it appears in snapshots; the engine is never start()ed in
    # these tests, so no live poll runs and nothing depends on the CI host.
    "host_metrics: {enabled: true, interval: 15}\n"
)


@pytest.fixture
def config():
    from app.config import Config
    return Config.load(write_config(WITH_HOST))

# ---------- snapshot ----------


def test_pending_panels_are_not_healthy(config, store):
    eng = Engine(config, store)
    snap = eng.snapshot()
    assert "healthy" not in snap["headline"]
    assert snap["counts"] == {"ok": 0, "warning": 0, "critical": 0, "unknown": 0}


def test_empty_result_is_unknown_not_green(config, store):
    eng = Engine(config, store)
    eng._ingest(make_panel("host_metrics", "Panel host", []))
    snap = eng.snapshot()
    panel = next(p for p in snap["panels"] if p["key"] == "host_metrics")
    assert panel["severity"] == UNKNOWN
    assert "healthy" not in snap["headline"]


def test_stale_ok_degrades_to_unknown(config, store):
    eng = Engine(config, store)
    panel = make_panel("host_metrics", "Panel host", [check("host.cpu", OK)])
    panel.ts = time.time() - 100_000
    eng._ingest(panel)
    snap = eng.snapshot()
    assert snap["overall"] == UNKNOWN
    assert snap["stale"] == 1
    assert snap["panels"][0]["checks"][0]["stale"] is True
    assert "healthy" not in snap["headline"]


def test_genuinely_healthy_says_so(config, store):
    eng = Engine(config, store)
    eng._ingest(make_panel("host_metrics", "Panel host", [check("host.cpu", OK)]))
    assert eng.snapshot()["headline"] == "Everything is healthy"


def test_snapshot_is_strict_json_serialisable_with_bad_metrics(config, store):
    """A NaN metric must not be able to break /api/status or the stream."""
    eng = Engine(config, store)
    bad = Check(id="x", name="x", severity=OK, value="?", metric=float("nan"), percent=float("inf"))
    panel = Panel(key="host_metrics", title="Panel host", checks=[bad], extra={"n": float("inf")}).resolve()
    eng._ingest(panel)
    json.dumps(eng.snapshot(), allow_nan=False)  # raises if anything non-finite survived
    assert bad.metric is None and bad.percent is None and bad.severity == UNKNOWN


def test_snapshot_reports_alert_health(config, store):
    eng = Engine(config, store)
    assert "alerts" in eng.snapshot()
    assert set(eng.snapshot()["alerts"]) >= {"pending", "delivered", "failed", "last_error"}


# ---------- store ----------


def test_mute_rejects_non_finite_and_clamps():
    st = Store(tempfile.mktemp(suffix=".db"))
    for bad in (math.inf, math.nan, -1, 0):
        with pytest.raises(ValueError):
            st.set_mute("x", "r", bad)
    st.set_mute("x", "r", 1e9)
    until = st.active_mutes()["x"]["until"]
    assert math.isfinite(until) and until <= time.time() + MAX_MUTE_HOURS * 3600 + 1
    st.close()


def test_limits_are_clamped(store):
    store.record_metrics([("a", float(i)) for i in range(10)])
    assert len(store.history("a", -1)) <= 5000
    assert len(store.history("a", 10**9)) <= 5000
    assert len(store.events(-5)) <= 500


def test_active_mutes_is_a_pure_read(store):
    store.set_mute("gone", "r", 0.0001)  # expires almost immediately
    time.sleep(0.5)
    assert "gone" not in store.active_mutes()
    # Row still exists until maintenance sweeps it — the read did not write.
    assert store.stats()["mutes"] == 1
    assert store.expire_mutes() == 1


def test_closed_store_raises_cleanly(store):
    store.close()
    with pytest.raises(StoreClosed):
        store.events()


def test_availability_bounded_by_first_seen(store):
    """A check seen an hour ago must not acquire a 24-hour history."""
    store.save_severities([("c", OK, "c", "p", OK)])
    # Backdate first_seen to one hour ago.
    with store._lock:
        store._conn.execute("UPDATE check_severity SET first_seen = ? WHERE check_id = 'c'", (time.time() - 3600,))
        store._conn.commit()
    result = store.availability("c", 24, OK)
    assert result["coverage_percent"] < 5
    assert result["ok_percent"] == 100.0
    assert result["unobserved_seconds"] >= 23 * 3600 - 5


def test_availability_never_seen_is_unobserved(store):
    result = store.availability("never", 24, "unknown")
    assert result["observed"] is False
    assert result["coverage_percent"] == 0


def test_indexes_used_for_prune_and_availability(store):
    with store._lock:
        plan = store._conn.execute("EXPLAIN QUERY PLAN DELETE FROM metrics WHERE ts < ?", (0,)).fetchall()
        assert "INDEX" in plan[0][-1]
        plan = store._conn.execute(
            "EXPLAIN QUERY PLAN SELECT new FROM events WHERE check_id = ? AND ts < ? ORDER BY ts DESC LIMIT 1",
            ("x", 0),
        ).fetchall()
        assert "idx_events_check_ts" in plan[0][-1]


# ---------- migrations ----------


def _legacy_db(path: str) -> None:
    c = sqlite3.connect(path)
    c.executescript(
        """
        CREATE TABLE metrics (check_id TEXT NOT NULL, ts REAL NOT NULL, value REAL NOT NULL);
        CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
          check_id TEXT NOT NULL, name TEXT NOT NULL, panel TEXT NOT NULL,
          old TEXT NOT NULL, new TEXT NOT NULL, value TEXT NOT NULL DEFAULT '');
        CREATE TABLE mutes (check_id TEXT PRIMARY KEY, reason TEXT NOT NULL DEFAULT '',
          until REAL, created REAL NOT NULL);
        """
    )
    now = time.time()
    c.executemany("INSERT INTO metrics VALUES (?,?,?)", [("host.cpu", now - i, float(i)) for i in range(50)])
    c.execute("INSERT INTO mutes VALUES (?,?,?,?)", ("host.mem", "known", now + 3600, now))
    c.commit()
    c.close()


def test_schema_version_matches_migration_count():
    assert len(MIGRATIONS) == SCHEMA_VERSION


def test_migration_preserves_data_and_is_idempotent():
    path = tempfile.mktemp(suffix=".db")
    _legacy_db(path)
    st = Store(path)
    assert st.schema_from == 0 and st.schema_to == SCHEMA_VERSION
    assert len(st.history("host.cpu", 100)) == 50
    assert "host.mem" in st.active_mutes()
    st.close()
    again = Store(path)
    assert again.schema_from == again.schema_to
    again.close()


def test_newer_schema_is_refused():
    path = tempfile.mktemp(suffix=".db")
    c = sqlite3.connect(path)
    c.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    c.commit()
    c.close()
    with pytest.raises(RuntimeError, match="newer version"):
        Store(path)


def test_interrupted_migration_rolls_back(monkeypatch):
    """A crash mid-migration must leave neither a half-schema nor a bumped version."""
    import app.migrations as m

    path = tempfile.mktemp(suffix=".db")
    c = sqlite3.connect(path)
    migrate(c)
    before = current_version(c)

    def boom(conn):
        conn.execute("CREATE TABLE half_done(a)")
        raise RuntimeError("power cut")

    monkeypatch.setattr(m, "MIGRATIONS", [*m.MIGRATIONS, boom])
    with pytest.raises(RuntimeError, match="power cut"):
        migrate(c)
    assert current_version(c) == before
    assert not c.execute("SELECT name FROM sqlite_master WHERE name='half_done'").fetchall()
    c.close()


# ---------- version ordering ----------


def test_prerelease_orders_before_final():
    from app.version import is_prerelease, parse_version

    assert parse_version("1.0.0-rc.1") < parse_version("1.0.0") < parse_version("1.0.1")
    assert parse_version("v9.0.0-test") < parse_version("9.0.0")
    assert parse_version("garbage") == (0, 0, 0, 0)
    assert is_prerelease("v1.0.0-rc.1") and not is_prerelease("v1.0.0")


def test_warning_constant_unchanged():
    # Guards against accidental renames that would silently break threshold config.
    assert WARNING == "warning"


def test_gone_check_is_not_reported_as_observed(store):
    """A check whose only history is 'gone' has no measured time."""
    store.record_event("g", "g", "p", "critical", "gone", "")
    result = store.availability("g", 24, "unknown")
    assert result["observed"] is False


def test_availability_counts_only_time_the_collector_was_polling(store):
    """Round 3, finding 22: a 12-hour gap in polling inside a 24-hour window
    cannot produce 100% observed coverage."""
    import time as _t
    now = _t.time()
    store.save_severities([("x.check", "ok", "x", "px", "ok")])
    # first_seen is "now" on insert; push it back a day so the window is open.
    with store._lock:
        store._conn.execute("UPDATE check_severity SET first_seen = ? WHERE check_id = 'x.check'", (now - 86400,))
        store._conn.commit()
    # Polled every 60s for the first 6h and the last 6h; nothing in between.
    for t in range(0, 6 * 3600, 60):
        store.record_observation("px", now - 86400 + t, 60)
    for t in range(18 * 3600, 24 * 3600, 60):
        store.record_observation("px", now - 86400 + t, 60)
    avail = store.availability("x.check", 24, "ok")
    assert avail["coverage_basis"] == "polled"
    assert avail["coverage_percent"] < 55, avail
    assert avail["coverage_percent"] > 45, avail
    assert avail["ok_percent"] == 100.0


def test_availability_without_a_poll_log_is_labelled_assumed(store):
    store.save_severities([("y.check", "ok", "y", "py", "ok")])
    avail = store.availability("y.check", 24, "ok")
    assert avail["coverage_basis"] == "assumed"
