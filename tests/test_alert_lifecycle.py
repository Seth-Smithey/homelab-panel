"""The alert lifecycle, as stated in app/scheduler.py's module docstring.

Each test here is a scenario one of the review passes reproduced as a defect.
If one of these fails, a real failure in someone's lab goes unreported.
"""

from __future__ import annotations

import asyncio

import pytest

from app.models import CRITICAL, OK, WARNING
from app.scheduler import Engine

from .conftest import check, ids_in, make_panel, write_config


@pytest.mark.asyncio
async def test_first_seen_failure_alerts(config, store, delivered):
    """Collectors that only emit a check when something is wrong (a failed
    Proxmox task, a disconnected Wazuh agent) must still alert."""
    eng = Engine(config, store)
    await eng.start()
    eng._ingest(make_panel("proxmox", "Compute", [check("pve.node.a", OK)]))
    eng._ingest(make_panel("proxmox", "Compute", [
        check("pve.node.a", OK),
        check("pve.task.UPID1", WARNING, "vzdump failed"),
    ]))
    await eng._drain_alerts_once()
    assert "pve.task.UPID1" in ids_in(delivered)
    await eng.stop()


@pytest.mark.asyncio
async def test_first_seen_healthy_is_quiet(config, store, delivered):
    eng = Engine(config, store)
    await eng.start()
    eng._ingest(make_panel("proxmox", "Compute", [check("pve.node.a", OK)]))
    eng._ingest(make_panel("proxmox", "Compute", [check("pve.node.a", OK), check("pve.node.b", OK)]))
    await eng._drain_alerts_once()
    assert not delivered
    await eng.stop()


@pytest.mark.asyncio
async def test_collector_outage_and_recovery_alert(config, store, delivered):
    eng = Engine(config, store)
    await eng.start()
    eng._ingest(make_panel("proxmox", "Compute", [check("pve.node.a", OK)]))
    eng._ingest(make_panel("proxmox", "Compute", [], error="Cannot reach 10.0.0.4"))
    await eng._drain_alerts_once()
    outage = [t for b in delivered for t in b if t["id"] == "panel.proxmox"]
    assert outage and outage[0]["old"] == OK and outage[0]["new"] == CRITICAL
    delivered.clear()
    eng._ingest(make_panel("proxmox", "Compute", [check("pve.node.a", OK)]))
    await eng._drain_alerts_once()
    recovery = [t for b in delivered for t in b if t["id"] == "panel.proxmox"]
    assert recovery and recovery[0]["old"] == CRITICAL and recovery[0]["new"] == OK
    await eng.stop()


@pytest.mark.asyncio
async def test_collector_reachability_is_persisted(config, store, delivered):
    eng = Engine(config, store)
    await eng.start()
    eng._ingest(make_panel("wazuh", "Endpoint", [], error="down"))
    saved = store.load_severities()
    assert saved["panel.wazuh"]["severity"] == CRITICAL
    await eng.stop()


@pytest.mark.asyncio
async def test_grace_delays_but_does_not_cancel(store, delivered):
    """A healthy check that turns critical DURING startup grace must be
    notified once grace ends — not silently never."""
    cfg_path = write_config(
        "config_version: 1\nsite: {name: t}\nserver: {host: 127.0.0.1, port: 8080, api_token: ''}\n"
        "poll: {default_interval: 5}\nhost_metrics: {enabled: false}\n"
        "alerts: {enabled: true, webhook_url: 'https://example.invalid/h', min_severity: warning, startup_grace: 1}\n"
    )
    from app.config import Config
    store.save_severities([("pve.guest.100", OK, "DC01", "Compute", OK)])
    eng = Engine(Config.load(cfg_path), store)
    await eng.start()
    assert eng._in_startup_grace()
    eng._ingest(make_panel("proxmox", "Compute", [check("pve.guest.100", CRITICAL, "DC01")]))
    await eng._drain_alerts_once()
    assert not delivered, "must not page during grace"
    await asyncio.sleep(1.3)
    await eng._drain_alerts_once()
    mine = [t for b in delivered for t in b if t["id"] == "pve.guest.100"]
    assert len(mine) == 1 and mine[0]["new"] == CRITICAL
    # Unchanged critical afterwards: no repeat.
    delivered.clear()
    eng._ingest(make_panel("proxmox", "Compute", [check("pve.guest.100", CRITICAL, "DC01")]))
    await eng._drain_alerts_once()
    assert not [t for b in delivered for t in b if t["id"] == "pve.guest.100"]
    await eng.stop()


def test_grace_zero_means_zero(store):
    from app.config import Config
    cfg_path = write_config(
        "config_version: 1\nserver: {host: 127.0.0.1, port: 8080}\nhost_metrics: {enabled: false}\n"
        "alerts: {enabled: false, startup_grace: 0}\n"
    )
    eng = Engine(Config.load(cfg_path), store)
    assert not eng._in_startup_grace()


@pytest.mark.asyncio
async def test_disappearance_recovers_then_recurrence_alerts_again(config, store, delivered):
    eng = Engine(config, store)
    await eng.start()
    eng._ingest(make_panel("wazuh", "Endpoint", [check("wazuh.agents", OK)]))
    eng._ingest(make_panel("wazuh", "Endpoint", [
        check("wazuh.agents", WARNING), check("wazuh.agent.web01", CRITICAL, "web01"),
    ]))
    await eng._drain_alerts_once()
    assert "wazuh.agent.web01" in ids_in(delivered)
    delivered.clear()

    # Recovered: the row vanishes from a SUCCESSFUL poll.
    eng._ingest(make_panel("wazuh", "Endpoint", [check("wazuh.agents", OK)]))
    await eng._drain_alerts_once()
    recov = [t for b in delivered for t in b if t["id"] == "wazuh.agent.web01"]
    assert recov and recov[0]["new"] == OK
    assert "wazuh.agent.web01" not in eng._state
    delivered.clear()

    # Same id fails again: a fresh failure, alerted again.
    eng._ingest(make_panel("wazuh", "Endpoint", [
        check("wazuh.agents", WARNING), check("wazuh.agent.web01", CRITICAL, "web01"),
    ]))
    await eng._drain_alerts_once()
    assert "wazuh.agent.web01" in ids_in(delivered)
    await eng.stop()


@pytest.mark.asyncio
async def test_failed_poll_infers_nothing(config, store, delivered):
    """A dead API did not fix your VM."""
    eng = Engine(config, store)
    await eng.start()
    eng._ingest(make_panel("wazuh", "Endpoint", [check("wazuh.agent.web01", CRITICAL)]))
    await eng._drain_alerts_once()
    delivered.clear()
    eng._ingest(make_panel("wazuh", "Endpoint", [], error="Cannot reach"))
    await eng._drain_alerts_once()
    assert "wazuh.agent.web01" in eng._state
    assert not [t for b in delivered for t in b if t["id"] == "wazuh.agent.web01"]
    await eng.stop()


@pytest.mark.asyncio
async def test_delivery_failure_retries_and_marks_notified_only_on_success(config, store, monkeypatch):
    attempts = {"n": 0}

    async def flaky(self, transitions):
        attempts["n"] += 1
        return (attempts["n"] >= 2), ("HTTP 500" if attempts["n"] < 2 else "")

    monkeypatch.setattr(Engine, "_deliver", flaky)
    eng = Engine(config, store)
    await eng.start()
    eng._ingest(make_panel("wazuh", "Endpoint", [check("wazuh.agents", CRITICAL)]))
    await eng._drain_alerts_once()
    assert eng._state["wazuh.agents"]["notified"] != CRITICAL
    assert eng._alert_health["pending"] == 1
    assert eng._alert_health["last_error"] == "HTTP 500"
    await eng._drain_alerts_once()          # still backing off: no attempt
    assert attempts["n"] == 1
    eng._alert_next = 0
    await eng._drain_alerts_once()
    assert eng._state["wazuh.agents"]["notified"] == CRITICAL
    assert eng._alert_health["pending"] == 0
    await eng.stop()


@pytest.mark.asyncio
async def test_state_survives_restart(config, store, delivered):
    eng = Engine(config, store)
    await eng.start()
    eng._ingest(make_panel("proxmox", "Compute", [check("pve.node.a", WARNING)]))
    await eng._drain_alerts_once()
    await eng.stop()
    eng2 = Engine(config, store)
    await eng2.start()
    assert eng2._state["pve.node.a"]["severity"] == WARNING
    assert eng2._state["pve.node.a"]["notified"] == WARNING
    await eng2.stop()


# ---------- found by the post-rewrite self-scan ----------


@pytest.mark.asyncio
async def test_check_that_resolved_while_down_is_forgotten_on_restart(config, store, delivered):
    """Disappearance detection must survive a restart, or a check that came
    back while the service was down is never resolved and its next failure
    is silent."""
    eng = Engine(config, store)
    await eng.start()
    eng._ingest(make_panel("wazuh", "Endpoint", [check("wazuh.agents", OK), check("wazuh.agent.web01", CRITICAL)]))
    await eng._drain_alerts_once()
    await eng.stop()
    delivered.clear()

    eng2 = Engine(config, store)
    await eng2.start()
    assert "wazuh.agent.web01" in eng2._state  # restored
    # First successful poll after restart: the agent is fine and absent.
    eng2._ingest(make_panel("wazuh", "Endpoint", [check("wazuh.agents", OK)]))
    await eng2._drain_alerts_once()
    recov = [t for b in delivered for t in b if t["id"] == "wazuh.agent.web01"]
    assert recov and recov[0]["new"] == OK, "no recovery alert after restart"
    assert "wazuh.agent.web01" not in eng2._state
    delivered.clear()
    eng2._ingest(make_panel("wazuh", "Endpoint", [check("wazuh.agents", WARNING), check("wazuh.agent.web01", CRITICAL)]))
    await eng2._drain_alerts_once()
    assert "wazuh.agent.web01" in ids_in(delivered), "repeat failure after restart was silent"
    await eng2.stop()


@pytest.mark.asyncio
async def test_undelivered_alert_survives_restart(store, delivered, monkeypatch):
    """A first-seen failure whose webhook never accepted it must still be
    notified after a restart — loading NULL notified as 'already notified'
    lost it for good."""
    from app.config import Config

    cfg = Config.load(write_config())
    failing = {"n": 0}

    async def never(self, transitions):
        failing["n"] += 1
        return False, "HTTP 503"

    monkeypatch.setattr(Engine, "_deliver", never)
    eng = Engine(cfg, store)
    await eng.start()
    eng._ingest(make_panel("proxmox", "Compute", [check("pve.task.X", WARNING)]))
    await eng._drain_alerts_once()
    assert eng._state["pve.task.X"]["notified"] is None
    await eng.stop()  # queue is lost with the process; the DB must carry the debt

    monkeypatch.setattr(Engine, "_deliver", lambda self, t: _ok(delivered, t))
    eng2 = Engine(cfg, store)
    await eng2.start()  # grace is 0 → reconcile runs immediately
    await asyncio.sleep(0.05)
    await eng2._drain_alerts_once()
    assert "pve.task.X" in ids_in(delivered), "undelivered alert vanished across restart"
    await eng2.stop()


async def _ok(sink, transitions):
    sink.append(transitions)
    return True, ""


@pytest.mark.asyncio
async def test_collector_unreachable_at_first_sight_alerts(config, store, delivered):
    """A fresh install with a wrong host or credential must page, not merely
    show a grey panel forever."""
    eng = Engine(config, store)
    await eng.start()
    eng._ingest(make_panel("proxmox", "Compute", [], error="401 from 10.0.0.4"))
    await eng._drain_alerts_once()
    mine = [t for b in delivered for t in b if t["id"] == "panel.proxmox"]
    assert mine and mine[0]["new"] == CRITICAL
    await eng.stop()


@pytest.mark.asyncio
async def test_webhook_outage_never_delivers_a_stale_state(config, store, monkeypatch):
    """Round 3, finding 5: fail the first critical, deliver the recovery, force
    the old retry. The receiver must never see "ok, then critical" — or the
    critical at all, since it never held while a webhook was reachable."""
    received: list[list[dict]] = []
    calls = {"n": 0}

    async def first_fails_then_ok(self, transitions):
        calls["n"] += 1
        if calls["n"] == 1:
            return False, "HTTP 500"
        received.append(transitions)
        return True, ""

    monkeypatch.setattr(Engine, "_deliver", first_fails_then_ok)
    eng = Engine(config, store)
    await eng.start()
    eng._ingest(make_panel("p", "P", [check("c", CRITICAL)]))
    await eng._drain_alerts_once()                       # critical: webhook down
    eng._ingest(make_panel("p", "P", [check("c", OK)]))
    eng._alert_next = 0
    await eng._drain_alerts_once()                       # webhook back
    eng._alert_next = 0
    await eng._drain_alerts_once()
    states = [t["new"] for batch in received for t in batch if t["id"] == "c"]
    assert CRITICAL not in states, f"stale outage delivered after recovery: {states}"
    assert eng._state["c"]["notified"] == OK
    await eng.stop()


@pytest.mark.asyncio
async def test_outage_then_worse_delivers_only_the_current_state(config, store, monkeypatch):
    received: list[list[dict]] = []
    calls = {"n": 0}

    async def first_fails_then_ok(self, transitions):
        calls["n"] += 1
        if calls["n"] == 1:
            return False, "HTTP 500"
        received.append(transitions)
        return True, ""

    monkeypatch.setattr(Engine, "_deliver", first_fails_then_ok)
    eng = Engine(config, store)
    await eng.start()
    eng._ingest(make_panel("p", "P", [check("c", WARNING)]))
    await eng._drain_alerts_once()          # WARNING fails
    eng._ingest(make_panel("p", "P", [check("c", CRITICAL)]))
    eng._alert_next = 0
    await eng._drain_alerts_once()          # one batch: new → critical
    eng._alert_next = 0
    await eng._drain_alerts_once()
    mine = [t for batch in received for t in batch if t["id"] == "c"]
    assert [t["new"] for t in mine] == [CRITICAL]
    assert eng._state["c"]["notified"] == CRITICAL
    await eng.stop()


@pytest.mark.asyncio
async def test_disappearance_during_grace_still_recovers_after_grace(store, delivered):
    """Round 3, finding 9: a notified critical that vanishes on the first
    successful poll during grace must still produce its recovery once grace
    ends — the tombstone carries the debt."""
    from app.config import Config
    cfg_path = write_config(
        "config_version: 1\nsite: {name: t}\nserver: {host: 127.0.0.1, port: 8080, api_token: ''}\n"
        "poll: {default_interval: 5}\nhost_metrics: {enabled: false}\n"
        "alerts: {enabled: true, webhook_url: 'https://example.invalid/h', min_severity: warning, startup_grace: 1}\n"
    )
    store.save_severities([("wazuh.agent.web01", CRITICAL, "web01", "wazuh", CRITICAL)])
    eng = Engine(Config.load(cfg_path), store)
    await eng.start()
    assert eng._in_startup_grace()
    eng._ingest(make_panel("wazuh", "Endpoint", [check("wazuh.agents", OK)]))  # web01 gone
    await eng._drain_alerts_once()
    assert not delivered
    await asyncio.sleep(1.3)
    await eng._drain_alerts_once()
    recov = [t for b in delivered for t in b if t["id"] == "wazuh.agent.web01"]
    assert recov and recov[0]["old"] == CRITICAL and recov[0]["new"] == OK
    assert "wazuh.agent.web01" not in eng._state
    assert "wazuh.agent.web01" not in store.load_severities()
    await eng.stop()


@pytest.mark.asyncio
async def test_recovery_debt_survives_restart(config, store, delivered, monkeypatch):
    """Round 3, finding 10: a recovery whose webhook failed is owed after a
    restart, delivered once, and the tombstone is then forgotten."""
    async def never(self, transitions):
        return False, "HTTP 503"

    eng = Engine(config, store)
    await eng.start()
    eng._ingest(make_panel("wazuh", "Endpoint", [check("wazuh.agent.web01", CRITICAL)]))
    await eng._drain_alerts_once()          # critical delivered
    delivered.clear()
    monkeypatch.setattr(Engine, "_deliver", never)
    eng._ingest(make_panel("wazuh", "Endpoint", []))   # gone; recovery owed
    await eng._drain_alerts_once()          # fails
    await eng.stop()
    saved = store.load_severities()
    assert saved["wazuh.agent.web01"]["severity"] == OK
    assert saved["wazuh.agent.web01"]["notified"] == CRITICAL

    monkeypatch.setattr(Engine, "_deliver", lambda self, t: _ok(delivered, t))
    eng2 = Engine(config, store)
    await eng2.start()
    eng2._ingest(make_panel("wazuh", "Endpoint", []))
    await eng2._drain_alerts_once()
    recov = [t for b in delivered for t in b if t["id"] == "wazuh.agent.web01"]
    assert len(recov) == 1 and recov[0]["new"] == OK
    assert "wazuh.agent.web01" not in eng2._state
    await eng2.stop()


@pytest.mark.asyncio
async def test_restored_state_of_disabled_collector_is_dropped(config, store, delivered, monkeypatch):
    monkeypatch.setattr(Engine, "_active_keys", lambda self: {c.key for c in self.collectors})
    store.save_severities([("pve.node.x", CRITICAL, "x", "proxmox", None)])
    eng = Engine(config, store)   # config enables no proxmox collector
    await eng.start()
    assert "pve.node.x" not in eng._state
    await eng._drain_alerts_once()
    assert not delivered
    assert "pve.node.x" not in store.load_severities()
    await eng.stop()


@pytest.mark.asyncio
async def test_partial_result_does_not_manufacture_recovery(config, store, delivered):
    """Round 3, finding 4: the agent list timed out but the summary succeeded.
    The disconnected agent did not recover; the gap is visible instead."""
    from app.models import Panel as P
    eng = Engine(config, store)
    await eng.start()
    eng._ingest(make_panel("wazuh", "Endpoint", [
        check("wazuh.agents", WARNING), check("wazuh.agent.web01", CRITICAL, "web01"),
    ]))
    await eng._drain_alerts_once()
    delivered.clear()

    partial = P(key="wazuh", title="Endpoint", checks=[check("wazuh.agents", WARNING)])
    partial.unread_section("wazuh.agent-list", "Agent list", "Agents", "timeout", "wazuh.agent.")
    eng._ingest(partial.resolve())
    await eng._drain_alerts_once()
    assert not [t for b in delivered for t in b if t["id"] == "wazuh.agent.web01"], "false recovery"
    assert eng._state["wazuh.agent.web01"]["severity"] == CRITICAL
    placeholder = [c for c in eng.panels["wazuh"].checks if c.id == "wazuh.agent-list"]
    assert placeholder and placeholder[0].severity == "unknown"

    # A complete poll without it IS a recovery.
    eng._ingest(make_panel("wazuh", "Endpoint", [check("wazuh.agents", OK)]))
    await eng._drain_alerts_once()
    recov = [t for b in delivered for t in b if t["id"] == "wazuh.agent.web01"]
    assert recov and recov[0]["new"] == OK
    await eng.stop()


@pytest.mark.asyncio
async def test_ephemeral_check_ageing_out_is_not_a_recovery(config, store, delivered):
    from app.models import Check
    eng = Engine(config, store)
    await eng.start()
    task = Check(id="pve.task.UPID:pve1:1", name="vzdump failed", severity=WARNING, ephemeral=True)
    eng._ingest(make_panel("proxmox", "Compute", [check("pve.tasks", WARNING), task]))
    await eng._drain_alerts_once()
    assert "pve.task.UPID:pve1:1" in ids_in(delivered)
    delivered.clear()
    eng._ingest(make_panel("proxmox", "Compute", [check("pve.tasks", OK)]))
    await eng._drain_alerts_once()
    assert not [t for b in delivered for t in b if t["id"] == "pve.task.UPID:pve1:1"]
    assert "pve.task.UPID:pve1:1" not in eng._state
    await eng.stop()


@pytest.mark.asyncio
async def test_unknown_is_not_notified_and_does_not_reset_debt(config, store, delivered):
    from app.models import UNKNOWN
    eng = Engine(config, store)
    await eng.start()
    eng._ingest(make_panel("wazuh", "Endpoint", [check("wazuh.manager", CRITICAL)]))
    await eng._drain_alerts_once()
    delivered.clear()
    eng._ingest(make_panel("wazuh", "Endpoint", [check("wazuh.manager", UNKNOWN)]))
    await eng._drain_alerts_once()
    assert not delivered, "a transition into unknown was notified"
    eng._ingest(make_panel("wazuh", "Endpoint", [check("wazuh.manager", CRITICAL)]))
    await eng._drain_alerts_once()
    assert not delivered, "unchanged critical re-alerted after an unknown blip"
    eng._ingest(make_panel("wazuh", "Endpoint", [check("wazuh.manager", OK)]))
    await eng._drain_alerts_once()
    assert [t["new"] for b in delivered for t in b if t["id"] == "wazuh.manager"] == [OK]
    await eng.stop()


@pytest.mark.asyncio
async def test_mute_defers_and_expiry_reports_a_fault_that_outlived_it(config, store, delivered):
    """Round 3, finding 11: a muted failure is not "notified"; when the mute
    lapses and it is still failing, it is reported then."""
    eng = Engine(config, store)
    await eng.start()
    store.set_mute("wazuh.agents", "maintenance", hours=0.5 / 3600)
    eng._ingest(make_panel("wazuh", "Endpoint", [check("wazuh.agents", CRITICAL)]))
    await eng._drain_alerts_once()
    assert not delivered
    assert eng._state["wazuh.agents"]["notified"] != CRITICAL
    await asyncio.sleep(0.6)
    store.expire_mutes()
    eng._ingest(make_panel("wazuh", "Endpoint", [check("wazuh.agents", CRITICAL)]))
    await eng._drain_alerts_once()
    assert "wazuh.agents" in ids_in(delivered)
    await eng.stop()


@pytest.mark.asyncio
async def test_muted_recovery_is_settled_silently(config, store, delivered):
    eng = Engine(config, store)
    await eng.start()
    eng._ingest(make_panel("wazuh", "Endpoint", [check("wazuh.agents", CRITICAL)]))
    await eng._drain_alerts_once()
    delivered.clear()
    store.set_mute("wazuh.agents", "known", hours=None)
    eng._ingest(make_panel("wazuh", "Endpoint", [check("wazuh.agents", OK)]))
    await eng._drain_alerts_once()
    assert not delivered
    assert eng._state["wazuh.agents"]["notified"] == OK
    await eng.stop()


@pytest.mark.asyncio
async def test_redirect_is_not_delivery(config, store, monkeypatch):
    import httpx

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None):
            return httpx.Response(302, headers={"location": "https://elsewhere"})

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    eng = Engine(config, store)
    ok, err = await eng._deliver([{"id": "c", "name": "c", "panel": "p", "old": OK, "new": CRITICAL, "value": "", "detail": ""}])
    assert not ok and "302" in err


@pytest.mark.asyncio
async def test_downgrade_below_floor_is_still_reported_after_a_delivered_critical(store, delivered):
    """Self-scan: with min_severity critical, critical → warning → ok must not
    leave the receiver's last word as "critical"."""
    from app.config import Config
    cfg_path = write_config(
        "config_version: 1\nsite: {name: t}\nserver: {host: 127.0.0.1, port: 8080, api_token: ''}\n"
        "poll: {default_interval: 5}\nhost_metrics: {enabled: false}\n"
        "alerts: {enabled: true, webhook_url: 'https://example.invalid/h', min_severity: critical, startup_grace: 0}\n"
    )
    eng = Engine(Config.load(cfg_path), store)
    await eng.start()
    eng._ingest(make_panel("p", "P", [check("disk", OK)]))
    eng._ingest(make_panel("p", "P", [check("disk", CRITICAL)]))
    await eng._drain_alerts_once()
    eng._ingest(make_panel("p", "P", [check("disk", WARNING)]))
    await eng._drain_alerts_once()
    eng._ingest(make_panel("p", "P", [check("disk", OK)]))
    await eng._drain_alerts_once()
    states = [t["new"] for b in delivered for t in b if t["id"] == "disk"]
    assert states == [CRITICAL, WARNING], states
    assert eng._state["disk"]["notified"] == OK  # the ok was settled silently, below the floor
    # A warning that was never preceded by a delivered critical stays silent.
    delivered.clear()
    eng._ingest(make_panel("p", "P", [check("disk2", WARNING)]))
    await eng._drain_alerts_once()
    assert not [t for b in delivered for t in b if t["id"] == "disk2"]
    await eng.stop()
