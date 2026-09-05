"""Pi-hole — blocking state, query volume, block rate. Handles v5 and v6 APIs."""

from __future__ import annotations

import time

from ..models import CRITICAL, OK, WARNING, Check, Panel
from .base import Collector, CollectorError


class PiholeCollector(Collector):
    key = "pihole"
    title = "DNS filtering"

    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self._sid = ""
        self._sid_ts = 0.0

    @property
    def base(self) -> str:
        return str(self.opt("host", "")).rstrip("/")

    async def _v6_sid(self, force: bool = False) -> str:
        if self._sid and not force and time.time() - self._sid_ts < 240:
            return self._sid
        r = await self.client().post(
            f"{self.base}/api/auth", json={"password": str(self.opt("password", ""))}
        )
        r.raise_for_status()
        self._sid = r.json()["session"]["sid"]
        self._sid_ts = time.time()
        return self._sid

    async def _v6(self) -> dict:
        # The cached sid can stop being valid before its 240s window elapses —
        # FTL restarted, session table evicted, another client took the slot.
        # Pi-hole then answers 401 with a JSON body that parses cleanly, so
        # without raise_for_status the panel reads "blocking disabled, 0
        # queries" and goes critical while Pi-hole is perfectly healthy.
        summary, blocking = await self._v6_fetch()
        if summary is None:
            summary, blocking = await self._v6_fetch(force=True)
        if summary is None:
            raise CollectorError(
                "Pi-hole rejected the session — check the password in your .env"
            )

        queries = summary.get("queries", {})
        gravity = summary.get("gravity", {})
        return {
            "enabled": (blocking or {}).get("blocking") == "enabled",
            "total": queries.get("total", 0),
            "blocked": queries.get("blocked", 0),
            "percent": queries.get("percent_blocked", 0.0),
            "domains": gravity.get("domains_being_blocked", 0),
            "clients": (summary.get("clients") or {}).get("active", 0),
        }

    async def _v6_fetch(self, force: bool = False) -> tuple[dict | None, dict | None]:
        """One attempt at the two v6 endpoints. Returns (None, None) on 401 so
        the caller can re-authenticate once before giving up."""
        sid = await self._v6_sid(force=force)
        headers = {"sid": sid}
        c = self.client()
        summary_r = await c.get(f"{self.base}/api/stats/summary", headers=headers)
        blocking_r = await c.get(f"{self.base}/api/dns/blocking", headers=headers)
        if summary_r.status_code == 401 or blocking_r.status_code == 401:
            self._sid = ""
            return None, None
        summary_r.raise_for_status()
        blocking_r.raise_for_status()
        return summary_r.json(), blocking_r.json()

    async def _v5(self) -> dict:
        token = str(self.opt("api_token", ""))
        url = f"{self.base}/admin/api.php?summaryRaw&auth={token}"
        r = await self.client().get(url)
        r.raise_for_status()
        d = r.json()
        return {
            "enabled": d.get("status") == "enabled",
            "total": int(d.get("dns_queries_today", 0)),
            "blocked": int(d.get("ads_blocked_today", 0)),
            "percent": float(d.get("ads_percentage_today", 0.0)),
            "domains": int(d.get("domains_being_blocked", 0)),
            "clients": int(d.get("unique_clients", 0)),
        }

    async def collect(self) -> Panel:
        panel = Panel(key=self.key, title=self.title)
        data = await (self._v6() if int(self.opt("api_version", 6)) >= 6 else self._v5())

        panel.checks.append(
            Check(
                id="pihole.blocking",
                name="Blocking",
                severity=OK if data["enabled"] else CRITICAL,
                value="enabled" if data["enabled"] else "disabled",
                detail=f"{data['domains']:,} domains on the blocklist",
                group="Service",
            )
        )

        # A dead resolver looks like zero queries; that is worth flagging.
        total = int(data["total"])
        panel.checks.append(
            Check(
                id="pihole.queries",
                name="Queries today",
                severity=OK if total > 0 else WARNING,
                value=f"{total:,}",
                detail=f"{int(data['blocked']):,} blocked · {data['clients']} clients",
                group="Traffic",
                metric=float(total),
                percent=round(float(data["percent"]), 1),
            )
        )

        panel.summary = f"{float(data['percent']):.1f}% of queries blocked"
        panel.extra = data
        return panel
