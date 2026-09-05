"""The engine: one independent loop per collector, a shared state cache,
transition detection, and outbound alerts.

Collectors never block each other. A hung UniFi controller cannot stop the
UPS panel from updating — the worst case is one panel going stale, which the
UI marks explicitly rather than silently showing old numbers as current.

Alert lifecycle, stated once so the code has something to be checked against:

* A check is **observed** at some severity every time its collector runs.
  Observation is recorded immediately and persisted.
* A check is **notified** at a severity when a webhook carrying that
  transition was accepted (HTTP 2xx). Observed and notified are tracked
  separately and persisted together, so suppressing a notification (startup
  grace, a mute) or failing to deliver one (webhook down) never makes the
  engine believe the world already knows.
* **Notification is a debt, not a message.** Every check whose observed
  severity differs from its notified severity is owed one notification: the
  transition from what was last delivered to what is true now. The worker
  settles all debts in one batch built from *current* state. A webhook outage
  spanning several transitions therefore delivers only the net result —
  never "ok, then critical" from a stale retry. Debts survive restarts
  because both sides live in the database.
* Startup grace *delays* settlement; it does not cancel it. A check that
  went critical during grace is still owed once grace ends. A check that went
  critical and recovered during grace owes nothing.
* A **mute** defers the debt. When a timed mute expires on a check that is
  still failing, the debt is settled then — a fault that outlived its mute
  is reported.
* Transitions below the configured severity floor are settled silently
  (marked notified without a webhook): the floor is policy, not a temporary
  condition. A transition *into* UNKNOWN is not a claim about the world and
  is never notified; the last notified severity stands until a real reading.
* A check that is **absent from a successful poll** of its own collector,
  having been present before, has resolved. It becomes a tombstone —
  observed OK — that owes a recovery if a failure was delivered, and is
  forgotten once it owes nothing, so the same id appearing bad later is a
  fresh failure. A *failed* poll infers nothing. A poll that declares part
  of itself **unread** (`Panel.unread`) infers nothing about checks under
  those prefixes: a timed-out detail endpoint did not fix your disk.
* A collector's own reachability is a check too (`panel.<key>`), persisted
  and notified under the same rules.
* State restored for a collector that is no longer enabled is dropped at
  startup: nothing will ever refresh it, so nothing should page for it.
* Delivery failures are retried with backoff, indefinitely, and the debt is
  never dropped; delivery health is visible in the snapshot. The one
  exception is an **ephemeral** check (a failed task, `Check.ephemeral`)
  that ages out of its window before its failure could be delivered: it is
  forgotten, because reporting a task failure that is already outside the
  window is stale news. It is still in the event log.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

import httpx

from .collectors import REGISTRY, Collector
from .config import Config
from .db import Store
from .models import CRITICAL, OK, UNKNOWN, WARNING, Panel, rank, worst
from .version import __version__

log = logging.getLogger("panel.scheduler")

# How many missed intervals before a panel is flagged stale in the UI.
STALE_FACTOR = 3

# Longest a failing collector will wait between attempts, in seconds.
BACKOFF_CEILING = 600

# How often the maintenance loop runs (prune, mute expiry).
MAINTENANCE_INTERVAL = 3600

# Alert delivery: the wait before each retry; the last value repeats.
ALERT_RETRY_DELAYS = (30, 120, 600)

# Synthetic states for panel-level reachability, so they flow through the
# same threshold logic as check transitions.
PANEL_OK = OK
PANEL_DOWN = CRITICAL

# Synthetic "previous" states recorded in the event log.
STATE_NEW = "new"    # first sighting
STATE_GONE = "gone"  # resolved by disappearing from a successful poll

BAD = (WARNING, CRITICAL)


class Engine:
    def __init__(self, cfg: Config, store: Store) -> None:
        self.cfg = cfg
        self.store = store
        self.collectors: list[Collector] = []
        self.panels: dict[str, Panel] = {}
        self._tasks: list[asyncio.Task] = []
        # {check_id: {"severity": observed, "notified": delivered, "first_seen": ts,
        #             "panel": panel key, "meta": {...}, "gone": bool}}
        self._state: dict[str, dict[str, Any]] = {}
        # {panel_key: set(check_id)} — what each collector published last time
        # it succeeded, so disappearance can be detected per collector.
        self._panel_checks: dict[str, set[str]] = {}
        self._panel_last_success: dict[str, float] = {}
        self._subscribers: set[asyncio.Queue] = set()
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._started_at = time.time()
        self._running = False
        # Delivery backoff for the outbox as a whole: one webhook, one outage.
        self._alert_attempt = 0
        self._alert_next = 0.0
        self._alert_health: dict[str, Any] = {
            "pending": 0,
            "delivered": 0,
            "failed": 0,
            "last_error": "",
            "last_delivered": None,
        }

        for cls in REGISTRY:
            if cfg.enabled(cls.key):
                self.collectors.append(cls(cfg))
            else:
                log.info("collector %s disabled in config", cls.key)

        for collector in self.collectors:
            self._refresh_locks[collector.key] = asyncio.Lock()

    # -- lifecycle -----------------------------------------------------

    async def start(self) -> None:
        self._running = True
        try:
            restored = self.store.load_severities()
            active = self._active_keys()
            retired = (
                [cid for cid, e in restored.items() if e.get("panel") not in active]
                if active is not None else []
            )
            for cid in retired:
                restored.pop(cid, None)
            if retired:
                log.info(
                    "dropping %d restored state(s) from collectors that are no longer enabled",
                    len(retired),
                )
                with contextlib.suppress(Exception):
                    self.store.forget_severities(retired)
            for entry in restored.values():
                entry.setdefault("meta", {})
                if entry.get("name"):
                    entry["meta"].setdefault("name", entry["name"])
            self._state = restored
            log.info("restored %d check states from the database", len(self._state))
        except Exception:  # noqa: BLE001 - never let this stop startup
            log.exception("could not restore check states; starting with an empty cache")

        for collector in self.collectors:
            self._tasks.append(asyncio.create_task(self._loop(collector)))
        self._tasks.append(asyncio.create_task(self._maintenance()))
        self._tasks.append(asyncio.create_task(self._alert_worker()))
        if self.cfg.get("heartbeat.enabled"):
            self._tasks.append(asyncio.create_task(self._heartbeat()))
        log.info("engine started with %d collectors", len(self.collectors))

    def _active_keys(self) -> set[str] | None:
        """Collector keys whose state may be restored; None means all of them."""
        return {c.key for c in self.collectors}

    async def stop(self) -> None:
        self._running = False

        # Stop the background loops first — including the alert worker — so
        # the final settlement below is the only thing touching state.
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(BaseException):
                await task
        self._tasks.clear()

        # Give owed alerts one more chance to land. Whatever is not delivered
        # stays owed in the database and is settled after the restart.
        if self._owed():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._drain_alerts_once(force=True), timeout=5)

        for collector in self.collectors:
            try:
                await collector.aclose()
            except Exception:  # noqa: BLE001
                log.debug("error closing %s client", collector.key, exc_info=True)

    async def _loop(self, collector: Collector) -> None:
        await asyncio.sleep(0.4 * self.collectors.index(collector))
        failures = 0
        while self._running:
            try:
                async with self._refresh_locks[collector.key]:
                    panel = await collector.run()
                self._ingest(panel)
                await self._broadcast()
                failures = 0 if not panel.error else failures + 1
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("loop error in %s", collector.key)
                failures += 1

            delay = max(5, collector.interval)
            if failures > 2:
                delay = min(delay * 2 ** min(failures - 2, 5), BACKOFF_CEILING)
            await asyncio.sleep(delay)

    async def _maintenance(self) -> None:
        while self._running:
            try:
                expired = self.store.expire_mutes()
                if expired:
                    log.info("expired %d mute(s)", expired)
                removed = self.store.prune(
                    int(self.cfg.get("poll.history_retention_days", 14) or 14)
                )
                if removed:
                    log.info("pruned %d history rows", removed)
            except Exception:  # noqa: BLE001
                log.exception("maintenance pass failed")
            await asyncio.sleep(MAINTENANCE_INTERVAL)

    # -- grace ---------------------------------------------------------

    def _grace_seconds(self) -> float:
        raw = self.cfg.get("alerts.startup_grace")
        if raw is None:
            return 120.0
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 120.0
        # 0 means zero. `or 120` made it mean 120.
        return max(0.0, value)

    def _in_startup_grace(self) -> bool:
        return (time.time() - self._started_at) < self._grace_seconds()

    # -- state ---------------------------------------------------------

    def _entry(self, check_id: str) -> dict[str, Any] | None:
        return self._state.get(check_id)

    def _interval_of(self, key: str) -> float:
        for collector in self.collectors:
            if collector.key == key:
                return float(collector.interval)
        return 60.0

    def _persist_row(self, check_id: str, entry: dict[str, Any]) -> tuple:
        meta = entry.get("meta") or {}
        return (
            check_id,
            entry["severity"],
            meta.get("name", check_id),
            entry.get("panel", ""),
            entry.get("notified"),
            bool(entry.get("ephemeral")),
        )

    def _ingest(self, panel: Panel) -> None:
        previous = self.panels.get(panel.key)
        self.panels[panel.key] = panel

        metrics = [(c.id, float(c.metric)) for c in panel.checks if c.metric is not None]
        self.store.record_metrics(metrics)

        persist: list[tuple] = []
        present_now: set[str] = set()

        for check in panel.checks:
            present_now.add(check.id)
            entry = self._entry(check.id)
            meta = {
                "name": check.name,
                "panel": panel.title,
                "value": check.value,
                "detail": check.detail,
            }

            if entry is None:
                # First sighting. A check that arrives already broken is a
                # transition from nothing (several collectors only emit a
                # check when something is wrong — one per failed task, one per
                # dead agent) and is owed a notification. A check that arrives
                # fine is not news: notified == observed from the start.
                bad = check.severity in BAD
                entry = {
                    "severity": check.severity,
                    "notified": None if bad else check.severity,
                    "first_seen": time.time(),
                    "panel": panel.key,
                    "meta": meta,
                    "ephemeral": check.ephemeral,
                }
                self._state[check.id] = entry
                if bad:
                    self.store.record_event(
                        check.id, check.name, panel.title, STATE_NEW, check.severity, check.value
                    )
                persist.append(self._persist_row(check.id, entry))
                continue

            old = entry["severity"]
            entry["meta"] = meta
            entry["panel"] = panel.key
            entry["ephemeral"] = check.ephemeral
            if entry.pop("gone", False):
                # It came back after being forgotten-in-progress: whatever it
                # owed as a tombstone, it now owes as a live check.
                old = STATE_GONE
            if old != check.severity:
                self.store.record_event(
                    check.id, check.name, panel.title, old, check.severity, check.value
                )
                entry["severity"] = check.severity
            persist.append(self._persist_row(check.id, entry))

        # Disappearance. Only a *successful* poll can say a check is gone, and
        # only for the parts of itself it actually read.
        if not panel.error:
            before = self._panel_checks.get(panel.key)
            if before is None:
                # First successful poll since startup: what this collector
                # published before the restart is in the restored state. Without
                # this, a check that resolved while the service was down is
                # never forgotten — no recovery alert, and its next failure is
                # "unchanged" and silent.
                before = {
                    cid for cid, entry in self._state.items()
                    if entry.get("panel") == panel.key and not cid.startswith("panel.")
                }
            retained: set[str] = set()
            forget: list[str] = []
            for gone_id in before - present_now:
                entry = self._entry(gone_id)
                if entry is None:
                    continue
                if any(gone_id.startswith(prefix) for prefix in panel.unread):
                    # Its section was not read this time. Keep it in the
                    # baseline; nothing is known about it.
                    retained.add(gone_id)
                    continue
                if entry.get("gone"):
                    # Already a tombstone (restart between disappearance and
                    # delivery). Nothing new to record.
                    if entry["severity"] == entry.get("notified"):
                        forget.append(gone_id)
                    else:
                        retained.add(gone_id)
                    continue
                last = entry["severity"]
                name = (entry.get("meta") or {}).get("name", gone_id)
                if last != OK:
                    # "ok → resolved" says nothing (and is what a tombstone
                    # restored across a restart would otherwise re-log).
                    self.store.record_event(gone_id, name, panel.title, last, STATE_GONE, "")
                if entry.get("ephemeral"):
                    # An event that aged out of its window did not "recover".
                    forget.append(gone_id)
                    continue
                entry["severity"] = OK
                entry["gone"] = True
                entry["meta"] = {
                    "name": name,
                    "panel": panel.title,
                    "value": "resolved",
                    "detail": "no longer reported by the collector",
                }
                if entry.get("notified") in BAD:
                    # Owes a recovery. Stays, as a tombstone, until delivered.
                    retained.add(gone_id)
                    persist.append(self._persist_row(gone_id, entry))
                else:
                    forget.append(gone_id)
            for gone_id in forget:
                self._state.pop(gone_id, None)
            self._panel_checks[panel.key] = present_now | retained
            self._panel_last_success[panel.key] = panel.ts
            try:
                self.store.record_observation(panel.key, panel.ts, self._interval_of(panel.key))
            except Exception:  # noqa: BLE001
                log.debug("could not record observation", exc_info=True)
            if forget:
                try:
                    self.store.forget_severities(forget)
                except Exception:  # noqa: BLE001
                    log.debug("could not forget vanished checks", exc_info=True)

        self._panel_transitions(panel, previous, persist)

        try:
            self.store.save_severities(persist)
        except Exception:  # noqa: BLE001 - persistence is best effort
            log.debug("could not persist check severities", exc_info=True)

    def _panel_transitions(
        self,
        panel: Panel,
        previous: Panel | None,
        persist: list[tuple],
    ) -> None:
        """Reachability of the collector itself, as an alertable state."""
        pseudo_id = f"panel.{panel.key}"
        new_state = PANEL_DOWN if panel.error else PANEL_OK
        entry = self._entry(pseudo_id)
        name = f"{panel.title} collector"
        meta = {
            "name": name,
            "panel": panel.title,
            "value": "unreachable" if panel.error else "reachable",
            "detail": panel.error or "the collector is answering again",
        }

        if entry is None:
            # First sighting, same rule as checks: arriving already broken is
            # a transition from nothing. A fresh install with a wrong host or
            # credential must page, not merely show a grey panel forever.
            bad = new_state == PANEL_DOWN
            entry = {
                "severity": new_state,
                "notified": None if bad else new_state,
                "first_seen": time.time(),
                "panel": panel.key,
                "meta": meta,
            }
            self._state[pseudo_id] = entry
            if bad:
                self.store.record_event(pseudo_id, name, panel.title, STATE_NEW, new_state, panel.error)
            persist.append(self._persist_row(pseudo_id, entry))
            return

        old_state = entry["severity"]
        entry["meta"] = meta
        entry["panel"] = panel.key
        if old_state != new_state:
            entry["severity"] = new_state
            self.store.record_event(
                pseudo_id, name, panel.title, old_state, new_state, panel.error or "reachable"
            )
        persist.append(self._persist_row(pseudo_id, entry))

    # -- snapshot ------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        mutes = self.store.active_mutes()
        panels = []
        counts = {OK: 0, WARNING: 0, CRITICAL: 0, UNKNOWN: 0}
        muted_count = 0
        pending = 0
        stale_titles: list[str] = []

        for collector in self.collectors:
            panel = self.panels.get(collector.key)
            if panel is None:
                placeholder = Panel(
                    key=collector.key, title=collector.title, summary="waiting for first poll"
                )
                data = placeholder.dict()
                data["interval"] = collector.interval
                data["pending"] = True
                data["muted_count"] = 0
                data["age"] = None
                data["last_success"] = None
                panels.append(data)
                pending += 1
                continue

            data = panel.dict()
            data["pending"] = False
            live_severities = []
            for check in data["checks"]:
                mute = mutes.get(check["id"])
                if mute:
                    check["muted"] = True
                    check["mute_reason"] = mute.get("reason", "")
                    check["mute_until"] = mute.get("until")
                    muted_count += 1
                else:
                    live_severities.append(check["severity"])

            stale = (now - panel.ts) > collector.interval * STALE_FACTOR
            if not data["error"]:
                if live_severities:
                    data["severity"] = worst(*live_severities)
                elif data["checks"]:
                    data["severity"] = OK
                else:
                    data["severity"] = UNKNOWN
                    if not data["summary"]:
                        data["summary"] = "answered, but reported nothing"

            # A stale result is not evidence of health. An OK reading from
            # three intervals ago says nothing about now, so a stale panel's
            # severity degrades to unknown rather than exporting a green it
            # cannot stand behind. The old readings stay visible, labelled.
            if stale:
                stale_titles.append(panel.title)
                if data["severity"] == OK:
                    data["severity"] = UNKNOWN
                for check in data["checks"]:
                    check["stale"] = True
                    if check["severity"] == OK:
                        # What the check said, and what it can stand behind
                        # now, are different things; expose both.
                        check["observed_severity"] = check["severity"]
                        check["severity"] = UNKNOWN

            for check in data["checks"]:
                if not check.get("muted"):
                    counts[check["severity"]] = counts.get(check["severity"], 0) + 1

            data["muted_count"] = len(data["checks"]) - len(live_severities)
            data["stale"] = stale
            data["age"] = round(now - panel.ts, 1)
            last_ok = self._panel_last_success.get(collector.key)
            data["last_success"] = round(now - last_ok, 1) if last_ok else None
            data["interval"] = collector.interval
            panels.append(data)

        overall = worst(*[p["severity"] for p in panels]) if panels else UNKNOWN
        return {
            "generated": now,
            "version": __version__,
            "uptime": round(now - self._started_at, 1),
            "site": self.cfg.section("site"),
            "overall": overall,
            "headline": self._headline(overall, counts, panels, pending, stale_titles),
            "counts": counts,
            "muted": muted_count,
            "pending": pending,
            "stale": len(stale_titles),
            "panels": sorted(panels, key=lambda p: -rank(p["severity"])),
            "panel_order": [c.key for c in self.collectors],
            "events": self.store.events(40),
            "alerts": {**self._alert_health, "pending": self._pending_count(mutes)},
        }

    def _headline(
        self,
        overall: str,
        counts: dict[str, int],
        panels: list[dict],
        pending: int,
        stale: list[str],
    ) -> str:
        broken = [p["title"] for p in panels if p.get("error")]
        empty = [
            p["title"]
            for p in panels
            if not p.get("pending") and not p.get("error") and not p["checks"]
        ]
        if overall == CRITICAL:
            n = counts.get(CRITICAL, 0)
            return "1 critical issue needs attention" if n == 1 else f"{n} critical issues need attention"
        if overall == WARNING:
            n = counts.get(WARNING, 0)
            return f"{n} thing{'s' if n != 1 else ''} to look at"
        if broken:
            return f"Can't reach {', '.join(broken[:3])}"
        if stale:
            return f"No fresh reading from {', '.join(stale[:3])}"
        if pending:
            return f"Waiting for the first poll from {pending} collector{'s' if pending != 1 else ''}"
        if empty:
            return f"No readings from {', '.join(empty[:3])}"
        if counts.get(UNKNOWN, 0) and not counts.get(OK, 0):
            return "Nothing has reported a usable reading yet"
        if counts.get(UNKNOWN, 0):
            n = counts[UNKNOWN]
            return f"{n} check{'s' if n != 1 else ''} could not be read"
        if not counts.get(OK, 0):
            return "No checks are reporting"
        return "Everything is healthy"

    # -- push ----------------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=4)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def _broadcast(self) -> None:
        if not self._subscribers:
            return
        payload = self.snapshot()
        for queue in list(self._subscribers):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(payload)

    async def refresh(self, key: str) -> Panel | None:
        for collector in self.collectors:
            if collector.key != key:
                continue
            async with self._refresh_locks[key]:
                panel = await collector.run()
            self._ingest(panel)
            await self._broadcast()
            return panel
        return None

    async def _heartbeat(self) -> None:
        conf = self.cfg.section("heartbeat")
        url = str(self.cfg.get("heartbeat.url", "") or "")
        try:
            interval = max(30, int(conf.get("interval", 300)))
        except (TypeError, ValueError):
            interval = 300
        if not url:
            log.warning("heartbeat enabled but no url set; not sending")
            return
        await asyncio.sleep(10)
        async with httpx.AsyncClient(timeout=10) as client:
            while self._running:
                try:
                    await client.get(url)
                except httpx.HTTPError as exc:
                    log.warning("heartbeat failed: %s", exc)
                await asyncio.sleep(interval)

    # -- alerts --------------------------------------------------------

    def _alerts_configured(self) -> bool:
        conf = self.cfg.section("alerts")
        return bool(conf.get("enabled")) and bool(self.cfg.get("alerts.webhook_url"))

    def _owed(
        self,
        mutes: dict[str, Any] | None = None,
        ignore_grace: bool = False,
        include_muted: bool = False,
    ) -> list[dict[str, str]]:
        """Every transition the world has not been told about, from current state.

        Muted checks are deferred (not listed). During startup grace nothing
        is listed unless `ignore_grace` — the snapshot uses that to show what
        is waiting.
        """
        if self._in_startup_grace() and not ignore_grace:
            return []
        if mutes is None:
            mutes = self.store.active_mutes()
        owed: list[dict[str, str]] = []
        for check_id, entry in self._state.items():
            observed = entry["severity"]
            notified = entry.get("notified")
            if observed in (notified, UNKNOWN):
                continue
            if check_id in mutes and not include_muted:
                continue
            meta = entry.get("meta") or {}
            owed.append(
                {
                    "id": check_id,
                    "name": meta.get("name", check_id),
                    "panel": meta.get("panel", ""),
                    "old": notified or STATE_NEW,
                    "new": observed,
                    "value": meta.get("value", ""),
                    "detail": meta.get("detail", ""),
                }
            )
        return owed

    def _pending_count(self, mutes: dict[str, Any]) -> int:
        """Transitions that WILL be sent (once grace ends and the webhook
        answers) — not debts that settle silently or have nowhere to go."""
        if not self._alerts_configured():
            return 0
        send, _ = self._notable(self._owed(mutes, ignore_grace=True))
        return len(send)

    def _notable(self, transitions: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        """Split owed transitions into (send these, settle these silently).

        Silent: below the severity floor; a recovery from a state that was
        itself below the floor; a recovery from a failure that was never
        delivered (telling someone a thing they never heard of is fixed is
        noise, and worse, reads as "it is down").
        """
        floor = rank(str(self.cfg.section("alerts").get("min_severity", WARNING)))
        send: list[dict[str, str]] = []
        silent: list[dict[str, str]] = []
        for t in transitions:
            new, old = t["new"], t["old"]
            delivered_above_floor = old in BAD and rank(old) >= floor
            if rank(new) >= floor or delivered_above_floor:
                # Either the new state clears the floor, or the receiver was
                # told about a state above the floor and must hear that it
                # changed — a downgrade (critical → warning under a critical
                # floor) included, or the last word they have is "critical".
                send.append(t)
            else:
                silent.append(t)
        return send, silent

    def _mark_notified(self, transitions: list[dict[str, str]]) -> None:
        rows: list[tuple] = []
        forget: list[str] = []
        for t in transitions:
            entry = self._entry(t["id"])
            if entry is None:
                continue
            entry["notified"] = t["new"]
            if entry.get("gone") and entry["severity"] == entry["notified"]:
                # The tombstone has paid its debt.
                forget.append(t["id"])
                continue
            rows.append(self._persist_row(t["id"], entry))
        for check_id in forget:
            self._state.pop(check_id, None)
            for present in self._panel_checks.values():
                present.discard(check_id)
        with contextlib.suppress(Exception):
            if rows:
                self.store.save_severities(rows)
            if forget:
                self.store.forget_severities(forget)

    async def _alert_worker(self) -> None:
        while self._running:
            try:
                await self._drain_alerts_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("alert worker error")
            await asyncio.sleep(2)

    async def _drain_alerts_once(self, force: bool = False) -> None:
        """Settle every debt that can be settled right now."""
        if self._in_startup_grace():
            self._alert_health["pending"] = len(self._owed(ignore_grace=True))
            return
        mutes = self.store.active_mutes()
        everything = self._owed(mutes, include_muted=True)
        # A muted check that recovered owes nothing: the mute said "do not tell
        # me about this", and its recovery is not news. A muted check that is
        # still failing keeps its debt for when the mute expires.
        muted_recoveries = [t for t in everything if t["id"] in mutes and t["new"] == OK]
        if muted_recoveries:
            self._mark_notified(muted_recoveries)
        owed = [t for t in everything if t["id"] not in mutes]
        if not owed:
            self._alert_attempt = 0
            self._alert_health["pending"] = 0
            return
        if not self._alerts_configured():
            # Nothing to deliver to; everything is "notified" by definition.
            self._mark_notified(owed)
            self._alert_health["pending"] = 0
            return

        send, silent = self._notable(owed)
        if silent:
            self._mark_notified(silent)
        if not send:
            self._alert_health["pending"] = 0
            return

        now = time.time()
        if now < self._alert_next and not force:
            self._alert_health["pending"] = len(send)
            return

        ok, error = await self._deliver(send)
        if ok:
            self._mark_notified(send)
            self._alert_attempt = 0
            self._alert_next = 0.0
            self._alert_health["delivered"] += 1
            self._alert_health["last_delivered"] = time.time()
            self._alert_health["last_error"] = ""
            # `failed` counts attempts during the current outage; the outage
            # is over.
            self._alert_health["failed"] = 0
        else:
            self._alert_attempt += 1
            self._alert_health["failed"] += 1
            self._alert_health["last_error"] = error
            delay = ALERT_RETRY_DELAYS[min(self._alert_attempt, len(ALERT_RETRY_DELAYS)) - 1]
            self._alert_next = time.time() + delay
            log.warning(
                "alert delivery failed (%s); %d transition(s) still owed, retry %d in %ds",
                error, len(send), self._alert_attempt, delay,
            )
        self._alert_health["pending"] = len(self._owed())

    async def _deliver(self, transitions: list[dict[str, str]]) -> tuple[bool, str]:
        """POST one batch. Returns (delivered, error). Delivered means 2xx."""
        webhook = str(self.cfg.get("alerts.webhook_url", "") or "")
        if not webhook or not transitions:
            return True, ""

        lines = []
        for t in transitions:
            arrow = "recovered" if t["new"] == OK else t["new"]
            lines.append(f"[{arrow}] {t['panel']} / {t['name']} — {t['value']} {t['detail']}".strip())
        body = {
            "site": self.cfg.get("site.name", "homelab"),
            "text": "\n".join(lines),
            "content": "\n".join(lines),
            "transitions": transitions,
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(webhook, json=body)
        except httpx.HTTPError as exc:
            return False, f"{type(exc).__name__}"
        if 200 <= response.status_code < 300:
            return True, ""
        if 300 <= response.status_code < 400:
            # Redirects are not followed on purpose: a POST that was redirected
            # was not received by the address you configured.
            return False, f"HTTP {response.status_code} redirect (configure the final URL)"
        return False, f"HTTP {response.status_code}"
