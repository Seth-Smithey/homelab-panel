"""UniFi (UDM-Pro) — WAN health, gateway/AP/switch state, client counts.

Supports UniFi OS local API keys (preferred) and username/password login.
All paths go through the /proxy/network prefix that UniFi OS uses.
"""

from __future__ import annotations

import httpx

from ..models import CRITICAL, OK, UNKNOWN, WARNING, Check, Panel
from .base import Collector
from .util import human_uptime

# UniFi device state codes. 1 is the only healthy one, but 4 and 5 are normal
# transient states — every device enters them after a settings change or a
# firmware push, and they clear on their own within a minute.
STATE_CONNECTED = 1
TRANSITIONAL_STATES = {4, 5}
STATE_TEXT = {
    0: "offline",
    1: "online",
    2: "pending adoption",
    4: "upgrading",
    5: "provisioning",
    6: "heartbeat missed",
}


class UnifiCollector(Collector):
    key = "unifi"
    title = "Network"

    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self._cookies: httpx.Cookies | None = None
        self._csrf: str = ""

    @property
    def base(self) -> str:
        return str(self.opt("host", "")).rstrip("/")

    async def _login(self) -> None:
        r = await self.client().post(
            f"{self.base}/api/auth/login",
            json={
                "username": self.opt("username"),
                "password": self.opt("password"),
                "rememberMe": True,
            },
        )
        r.raise_for_status()
        self._cookies = r.cookies
        self._csrf = r.headers.get("x-csrf-token", "")

    async def _get(self, path: str, retry: bool = True):
        site = self.opt("site", "default")
        url = f"{self.base}/proxy/network/api/s/{site}{path}"
        headers: dict[str, str] = {}
        cookies = None

        if self.opt("auth_mode", "api_key") == "api_key":
            headers["X-API-KEY"] = str(self.opt("api_key", ""))
            headers["Accept"] = "application/json"
        else:
            if self._cookies is None:
                await self._login()
            cookies = self._cookies
            if self._csrf:
                headers["X-CSRF-Token"] = self._csrf

        r = await self.client().get(url, headers=headers, cookies=cookies)
        if r.status_code in (401, 403) and retry and self.opt("auth_mode") == "login":
            self._cookies = None
            return await self._get(path, retry=False)
        r.raise_for_status()
        return r.json().get("data", [])

    async def collect(self) -> Panel:
        panel = Panel(key=self.key, title=self.title)
        latency_warn = float(self.opt("wan_latency_warn_ms", 60))
        watch = {str(d).lower() for d in self.opt("watch_devices", [])}

        health = await self._get("/stat/health")
        subsystems = {h.get("subsystem"): h for h in health}

        wan = subsystems.get("wan", {})
        wan_up = wan.get("status") == "ok"
        latency = wan.get("latency")
        sev = OK if wan_up else CRITICAL
        if wan_up and latency and float(latency) > latency_warn:
            sev = WARNING
        panel.checks.append(
            Check(
                id="unifi.wan",
                name="Internet",
                severity=sev,
                value="up" if wan_up else "down",
                detail=(
                    f"{wan.get('wan_ip', 'no IP')} · {latency or '—'} ms"
                    f" · ↓{_mbps(wan.get('rx_bytes-r'))} ↑{_mbps(wan.get('tx_bytes-r'))}"
                ),
                group="WAN",
                metric=float(latency) if latency else None,
                metric_unit="ms",
            )
        )

        for name, label in (("wlan", "Wireless"), ("lan", "Wired"), ("www", "DNS/WWW")):
            sub = subsystems.get(name)
            if not sub:
                continue
            ok = sub.get("status") == "ok"
            users = sub.get("num_user") or sub.get("num_sta") or 0
            panel.checks.append(
                Check(
                    id=f"unifi.{name}",
                    name=label,
                    severity=OK if ok else WARNING,
                    value=sub.get("status", "unknown"),
                    detail=f"{users} clients" if users else "",
                    group="Subsystems",
                    metric=float(users) if users else None,
                )
            )

        devices = await self._get("/stat/device")
        offline = 0
        for d in devices:
            name = d.get("name") or d.get("model") or d.get("mac")
            state = d.get("state")
            connected = state == STATE_CONNECTED
            # Upgrading and provisioning are normal, transient states that
            # every device passes through after a settings change or firmware
            # push. Treating them as offline fires an alert storm across the
            # whole estate, then a second one a minute later on recovery.
            transitional = state in TRANSITIONAL_STATES
            offline += int(not connected and not transitional)
            watched = str(name).lower() in watch
            if connected:
                sev = OK
            elif transitional:
                sev = UNKNOWN
            else:
                sev = CRITICAL if watched else WARNING
            bits = [d.get("model", "")]
            if connected:
                bits.append(f"up {human_uptime(d.get('uptime'))}")
                if d.get("upgradable"):
                    bits.append("update available")
            panel.checks.append(
                Check(
                    id=f"unifi.dev.{d.get('mac')}",
                    name=str(name),
                    severity=sev,
                    value=STATE_TEXT.get(state, "offline" if not connected else "online"),
                    detail=" · ".join(b for b in bits if b),
                    group="Devices",
                )
            )

        clients = await self._get("/stat/sta")
        wired = sum(1 for c in clients if c.get("is_wired"))
        panel.checks.append(
            Check(
                id="unifi.clients",
                name="Connected clients",
                severity=OK,
                value=str(len(clients)),
                detail=f"{wired} wired · {len(clients) - wired} wireless",
                group="Subsystems",
                metric=float(len(clients)),
            )
        )

        panel.summary = (
            f"{len(devices) - offline}/{len(devices)} devices up · {len(clients)} clients"
        )
        panel.extra = {
            "clients_total": len(clients),
            "clients_wired": wired,
            "devices_offline": offline,
            "wan_ip": wan.get("wan_ip", ""),
        }
        return panel


def _mbps(rate: float | int | None) -> str:
    if not rate:
        return "0.0 Mb/s"
    return f"{float(rate) * 8 / 1_000_000:.1f} Mb/s"
