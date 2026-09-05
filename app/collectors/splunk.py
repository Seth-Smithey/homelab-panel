"""Splunk Enterprise — server health, license headroom, and index fullness.

Index fullness is the quiet failure mode: nothing looks broken, the index
just silently starts rolling your oldest data to frozen.
"""

from __future__ import annotations

import time

from ..models import CRITICAL, OK, WARNING, Check, Panel
from .base import Collector
from .util import human_bytes, human_uptime


class SplunkCollector(Collector):
    key = "splunk"
    title = "Splunk"

    @property
    def base(self) -> str:
        return str(self.opt("host", "")).rstrip("/")

    async def _get(self, path: str, params: dict | None = None):
        query = {"output_mode": "json", "count": 0}
        query.update(params or {})
        r = await self.client().get(
            f"{self.base}{path}",
            params=query,
            auth=(str(self.opt("username")), str(self.opt("password"))),
        )
        r.raise_for_status()
        return r.json()

    async def collect(self) -> Panel:
        panel = Panel(key=self.key, title=self.title)

        info = await self._get("/services/server/info")
        content = (info.get("entry") or [{}])[0].get("content", {})
        startup = float(content.get("startup_time", 0) or 0)
        uptime = time.time() - startup if startup else None
        panel.checks.append(
            Check(
                id="splunk.server",
                name="Indexer",
                severity=OK,
                value=str(content.get("serverName", "up")),
                detail=(
                    f"v{content.get('version', '?')} · {content.get('os_name', 'linux')}"
                    f" · up {human_uptime(uptime)}"
                ),
                group="Service",
                metric=round(uptime / 86400, 2) if uptime else None,
                metric_unit="d",
            )
        )

        # Splunk's own health tree: green/yellow/red, straight from the source.
        try:
            health = await self._get("/services/server/health/splunkd")
            entry = (health.get("entry") or [{}])[0].get("content", {})
            colour = str(entry.get("health", "unknown")).lower()
            sev = {"green": OK, "yellow": WARNING, "red": CRITICAL}.get(colour, WARNING)
            unhealthy = [
                name
                for name, node in entry.items()
                if isinstance(node, dict)
                and str(node.get("health", "")).lower() in ("red", "yellow")
            ]
            panel.checks.append(
                Check(
                    id="splunk.health",
                    name="Health report",
                    severity=sev,
                    value=colour,
                    detail=", ".join(unhealthy[:4]) if unhealthy else "all features green",
                    group="Service",
                )
            )
        except Exception as exc:  # noqa: BLE001 - endpoint shape varies by version
            panel.unread_section("splunk.health", "Health report", "Service", str(exc))

        # License consumption against the daily quota.
        try:
            pools = await self._get("/services/licenser/pools")
            used = quota = 0
            for entry in pools.get("entry", []):
                c = entry.get("content", {})
                pool_quota = str(c.get("effective_quota") or c.get("quota") or "0")
                # Only count usage from pools that actually carry a quota.
                # Summing usage across every pool while dividing by the quota
                # of only some of them inflates the percentage — an unlimited
                # (MAX) pool's volume would be charged against a sized pool's
                # allowance and page for a breach that isn't happening.
                if pool_quota.upper() in ("MAX", "0"):
                    continue
                quota += int(pool_quota)
                used += int(c.get("used_bytes", 0) or 0)
            if quota:
                pct = used / quota * 100
                warn = float(self.opt("license_warn_pct", 80))
                crit = float(self.opt("license_crit_pct", 95))
                sev = CRITICAL if pct >= crit else WARNING if pct >= warn else OK
                panel.checks.append(
                    Check(
                        id="splunk.license",
                        name="License volume",
                        severity=sev,
                        value=f"{pct:.0f}% of quota",
                        detail=f"{human_bytes(used)} of {human_bytes(quota)} used today",
                        group="Licensing",
                        metric=round(pct, 1),
                        metric_unit="%",
                        percent=round(pct, 1),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            panel.unread_section("splunk.license", "License volume", "Licensing", str(exc))

        # Index fullness — data starts freezing off the end when this hits 100%.
        try:
            watch = {str(i).lower() for i in self.opt("watch_indexes", [])}
            warn = float(self.opt("index_warn_pct", 85))
            crit = float(self.opt("index_crit_pct", 95))
            indexes = await self._get("/services/data/indexes")
            for entry in indexes.get("entry", []):
                name = str(entry.get("name", ""))
                if name.startswith("_") and name not in ("_internal",):
                    continue
                if watch and name.lower() not in watch:
                    continue
                c = entry.get("content", {})
                if c.get("disabled"):
                    continue
                current_mb = float(c.get("currentDBSizeMB", 0) or 0)
                max_mb = float(c.get("maxTotalDataSizeMB", 0) or 0)
                events = int(c.get("totalEventCount", 0) or 0)
                if max_mb <= 0:
                    continue
                pct = current_mb / max_mb * 100
                sev = CRITICAL if pct >= crit else WARNING if pct >= warn else OK
                panel.checks.append(
                    Check(
                        id=f"splunk.index.{name}",
                        name=f"Index {name}",
                        severity=sev,
                        value=f"{pct:.0f}% full",
                        detail=(
                            f"{human_bytes(current_mb * 1024 ** 2)} of "
                            f"{human_bytes(max_mb * 1024 ** 2)} · {events:,} events"
                        ),
                        group="Indexes",
                        metric=round(pct, 1),
                        metric_unit="%",
                        percent=round(pct, 1),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            panel.unread_section(
                "splunk.indexes", "Indexes", "Indexes", str(exc), "splunk.index."
            )

        panel.summary = f"v{content.get('version', '?')} · up {human_uptime(uptime)}"
        return panel
