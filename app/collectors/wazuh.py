"""Wazuh — manager daemon status and agent connection health."""

from __future__ import annotations

import hashlib
import logging
import time

import httpx

from ..models import CRITICAL, OK, UNKNOWN, WARNING, Check, Panel
from .base import Collector

log = logging.getLogger("panel.collector.wazuh")


# Manager daemons that ship disabled in a stock Wazuh install. Reporting these
# as stopped pins the panel critical forever, which trains you to ignore it.
# Anything not in this set is expected to be running.
OPTIONAL_DAEMONS = {
    "wazuh-agentlessd",
    "wazuh-authd",
    "wazuh-clusterd",
    "wazuh-csyslogd",
    "wazuh-dbd",
    "wazuh-integratord",
    "wazuh-maild",
    "wazuh-reportd",
}


def _stable_id(text: str) -> str:
    """A check id that survives a restart.

    str.__hash__ is salted per process, so ids built from it change on every
    restart — orphaning mutes and metric history for the same logical check.
    """
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:10]


class WazuhCollector(Collector):
    key = "wazuh"
    title = "Endpoint security"

    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self._token = ""
        self._token_ts = 0.0

    @property
    def base(self) -> str:
        return str(self.opt("host", "")).rstrip("/")

    async def _auth(self) -> str:
        # Wazuh JWTs last 15 minutes by default; refresh at 10.
        if self._token and time.time() - self._token_ts < 600:
            return self._token
        r = await self.client().post(
            f"{self.base}/security/user/authenticate",
            auth=(str(self.opt("username")), str(self.opt("password"))),
        )
        r.raise_for_status()
        self._token = r.json()["data"]["token"]
        self._token_ts = time.time()
        return self._token

    async def _get(self, path: str, retry: bool = True):
        token = await self._auth()
        r = await self.client().get(
            f"{self.base}{path}", headers={"Authorization": f"Bearer {token}"}
        )
        if r.status_code == 401 and retry:
            self._token = ""
            return await self._get(path, retry=False)
        r.raise_for_status()
        return r.json().get("data", {})

    async def collect(self) -> Panel:
        panel = Panel(key=self.key, title=self.title)
        min_active = int(self.opt("min_active_agents", 1))

        summary = await self._get("/agents/summary/status")
        connection = summary.get("connection", summary)
        active = int(connection.get("active", 0))
        disconnected = int(connection.get("disconnected", 0))
        never = int(connection.get("never_connected", 0))
        pending = int(connection.get("pending", 0))
        total = int(connection.get("total", active + disconnected + never + pending))

        sev = OK
        if active < min_active:
            sev = CRITICAL
        elif disconnected or never:
            sev = WARNING
        panel.checks.append(
            Check(
                id="wazuh.agents",
                name="Agents reporting",
                severity=sev,
                value=f"{active}/{total}",
                detail=(
                    f"{disconnected} disconnected · {pending} pending · {never} never connected"
                ),
                group="Agents",
                metric=float(active),
                percent=round(active / total * 100, 1) if total else None,
            )
        )

        # Name the agents that aren't reporting — that's the actionable part.
        # Paginated: an inventory cut off at a page boundary would let an
        # agent past the cut-off "recover" by never being listed.
        try:
            agents = await self._all_agents()
        except httpx.HTTPError as exc:
            panel.unread_section(
                "wazuh.agent-list", "Agent list", "Agents",
                f"could not list agents: {exc}", "wazuh.agent.",
            )
        else:
            for agent in agents:
                status = agent.get("status", "unknown")
                if status == "active":
                    continue
                panel.checks.append(
                    Check(
                        id=f"wazuh.agent.{agent.get('name')}",
                        name=str(agent.get("name")),
                        # A box that was reporting and has gone dark is the
                        # actionable case. One that never enrolled is usually a
                        # key generated ahead of a build.
                        severity=CRITICAL if status == "disconnected" else WARNING,
                        value=status.replace("_", " "),
                        detail=f"last seen {agent.get('lastKeepAlive', 'never')}",
                        group="Agents",
                    )
                )

        try:
            status = await self._get("/manager/status")
        except httpx.HTTPError as exc:
            panel.unread_section(
                "wazuh.manager", "Manager daemons", "Manager", f"could not read daemon status: {exc}"
            )
        else:
            # .get(key, default) does not help when the key is present but the
            # list is empty — which is what an RBAC-limited user gets back on a
            # 200 response. No daemon map means nothing is known, not "all
            # running".
            items = status.get("affected_items") or []
            daemons = items[0] if items and isinstance(items[0], dict) else {}
            if not daemons:
                panel.checks.append(
                    Check(
                        id="wazuh.manager",
                        name="Manager daemons",
                        severity=UNKNOWN,
                        value="no data",
                        detail="the API returned no daemon states — the user may lack manager:read",
                        group="Manager",
                    )
                )
            else:
                stopped = [
                    k for k, v in daemons.items() if v != "running" and k not in OPTIONAL_DAEMONS
                ]
                idle = [
                    k for k, v in daemons.items() if v != "running" and k in OPTIONAL_DAEMONS
                ]
                detail = ", ".join(stopped) if stopped else ""
                if idle:
                    detail = (detail + " · " if detail else "") + f"{len(idle)} optional not running"
                panel.checks.append(
                    Check(
                        id="wazuh.manager",
                        name="Manager daemons",
                        severity=OK if not stopped else CRITICAL,
                        value="all running" if not stopped else f"{len(stopped)} stopped",
                        detail=detail,
                        group="Manager",
                    )
                )

        panel.summary = f"{active} of {total} agents active"
        panel.extra = {"active": active, "total": total, "disconnected": disconnected}

        # The indexer is optional and separate from the manager. If it is down,
        # that must not discard the agent and daemon checks already collected.
        try:
            await self._add_alerts(panel)
        except Exception as exc:  # noqa: BLE001 - optional section, never fatal
            log.warning("wazuh indexer section skipped: %s", exc)
            panel.unread_section(
                "wazuh.indexer", "Indexer", "Alerts", f"unreachable: {exc}",
                "wazuh.alerts", "wazuh.rule.",
            )
        return panel

    async def _all_agents(self) -> list[dict]:
        """Every agent, following the API's offset pagination to the end."""
        page = 500
        offset = 0
        agents: list[dict] = []
        for _ in range(200):  # 100k agents; a bound, not a limit anyone reaches
            data = await self._get(
                f"/agents?select=name,status,lastKeepAlive,version&limit={page}&offset={offset}"
            )
            items = data.get("affected_items") or []
            agents.extend(items)
            total = int(data.get("total_affected_items", len(agents)) or 0)
            offset += len(items)
            if not items or offset >= total:
                break
        return agents

    async def _add_alerts(self, panel: Panel) -> None:
        """Recent alerts from the Wazuh Indexer, bucketed by rule level.

        The manager API doesn't serve alerts — they live in the indexer. This
        is optional: leave the indexer block out and the panel skips it.
        """
        indexer = self.opt("indexer") or {}
        if not indexer.get("enabled"):
            return

        window = int(indexer.get("window_minutes", 60))
        notable = int(indexer.get("notable_level", 10))
        warn_at = int(indexer.get("warn_count", 1))
        crit_at = int(indexer.get("crit_count", 10))
        base = str(indexer.get("host", "")).rstrip("/")

        query = {
            "size": 0,
            "query": {"range": {"@timestamp": {"gte": f"now-{window}m"}}},
            "aggs": {
                "levels": {
                    "terms": {"field": "rule.level", "size": 20, "order": {"_key": "desc"}}
                },
                "top_rules": {
                    # Wide enough that a rule oscillating around the cut-off
                    # does not appear, vanish and reappear (each reappearance
                    # would be a fresh alert).
                    "terms": {"field": "rule.description", "size": 25},
                    "aggs": {"max_level": {"max": {"field": "rule.level"}}},
                },
            },
        }

        r = await self.client(verify=bool(indexer.get("verify_ssl", False))).post(
            f"{base}/wazuh-alerts-*/_search",
            json=query,
            auth=(str(indexer.get("username", "")), str(indexer.get("password", ""))),
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
        body = r.json()

        buckets = body.get("aggregations", {}).get("levels", {}).get("buckets", [])
        total = sum(int(b.get("doc_count", 0)) for b in buckets)
        high = sum(int(b["doc_count"]) for b in buckets if int(b["key"]) >= notable)

        if high >= crit_at:
            sev = CRITICAL
        elif high >= warn_at:
            sev = WARNING
        else:
            sev = OK

        panel.checks.append(
            Check(
                id="wazuh.alerts",
                name=f"Alerts (level {notable}+)",
                severity=sev,
                value=str(high),
                detail=f"{total:,} alerts total in the last {window}m",
                group="Alerts",
                metric=float(high),
            )
        )

        for rule in body.get("aggregations", {}).get("top_rules", {}).get("buckets", []):
            level = int((rule.get("max_level") or {}).get("value") or 0)
            if level < notable:
                continue
            description = str(rule.get("key", ""))[:70]
            panel.checks.append(
                Check(
                    id=f"wazuh.rule.{_stable_id(description)}",
                    name=description,
                    severity=WARNING if level < 12 else CRITICAL,
                    value=f"{rule.get('doc_count', 0)}×",
                    detail=f"rule level {level}",
                    group="Alerts",
                    metric=float(rule.get("doc_count", 0)),
                )
            )
