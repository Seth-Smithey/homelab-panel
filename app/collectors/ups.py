"""UPS — speaks the NUT upsd protocol directly over TCP 3493.

No client library needed; LIST VAR is a handful of lines of plain text.
"""

from __future__ import annotations

import asyncio
import shlex

from ..models import CRITICAL, OK, UNKNOWN, WARNING, Check, Panel
from .base import Collector, CollectorError

# NUT status flags, in the order we want to report them.
STATUS_TEXT = {
    "OL": ("on line", OK),
    "OB": ("on battery", CRITICAL),
    "LB": ("low battery", CRITICAL),
    "HB": ("high battery", WARNING),
    "RB": ("replace battery", WARNING),
    "CHRG": ("charging", OK),
    "DISCHRG": ("discharging", WARNING),
    "BYPASS": ("on bypass", WARNING),
    "CAL": ("calibrating", WARNING),
    "OFF": ("output off", CRITICAL),
    "OVER": ("overloaded", CRITICAL),
    "TRIM": ("trimming voltage", WARNING),
    "BOOST": ("boosting voltage", WARNING),
    "ALARM": ("alarm", CRITICAL),
}


class UpsCollector(Collector):
    key = "ups"
    title = "Power"

    async def _list_vars(self) -> dict[str, str]:
        host = str(self.opt("host", "127.0.0.1"))
        port = int(self.opt("port", 3493))
        ups = str(self.opt("ups_name", "ups"))

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=6
        )
        try:
            async def send(line: str) -> str:
                writer.write(f"{line}\n".encode())
                await writer.drain()
                return (await asyncio.wait_for(reader.readline(), timeout=6)).decode().strip()

            if self.opt("username"):
                resp = await send(f"USERNAME {self.opt('username')}")
                if not resp.startswith("OK"):
                    raise CollectorError(f"upsd rejected the username: {resp}")
                resp = await send(f"PASSWORD {self.opt('password')}")
                if not resp.startswith("OK"):
                    raise CollectorError("upsd rejected the password")

            writer.write(f"LIST VAR {ups}\n".encode())
            await writer.drain()

            variables: dict[str, str] = {}
            while True:
                raw = await asyncio.wait_for(reader.readline(), timeout=6)
                if not raw:
                    break
                line = raw.decode().strip()
                if line.startswith("ERR"):
                    raise CollectorError(f"upsd: {line}")
                if line.startswith("END LIST"):
                    break
                if line.startswith("VAR "):
                    parts = shlex.split(line)
                    if len(parts) >= 4:
                        variables[parts[2]] = parts[3]
            try:
                writer.write(b"LOGOUT\n")
                await writer.drain()
            except Exception:
                pass
            return variables
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def collect(self) -> Panel:
        panel = Panel(key=self.key, title=self.title)
        v = await self._list_vars()
        if not v:
            raise CollectorError(f"upsd knows no UPS named {self.opt('ups_name')!r}")

        flags = v.get("ups.status", "").split()
        # No status flags at all is not healthy — it usually means the driver
        # has lost contact with the device while still serving a partial
        # variable set. Reporting OK there would render "unknown" in green.
        worst_sev = OK if flags else UNKNOWN
        words = []
        for flag in flags:
            text, sev = STATUS_TEXT.get(flag, (flag.lower(), WARNING))
            words.append(text)
            if sev == CRITICAL or (sev == WARNING and worst_sev == OK):
                worst_sev = sev
        panel.checks.append(
            Check(
                id="ups.status",
                name="Mains power",
                severity=worst_sev,
                value=", ".join(words) or "unknown",
                detail=f"{v.get('ups.model', v.get('device.model', 'UPS'))}"
                + (f" · {v.get('input.voltage')} V in" if v.get("input.voltage") else ""),
                group="Status",
            )
        )

        charge = _f(v.get("battery.charge"))
        if charge is not None:
            warn = float(self.opt("battery_warn_pct", 60))
            sev = CRITICAL if charge < warn / 2 else WARNING if charge < warn else OK
            panel.checks.append(
                Check(
                    id="ups.battery",
                    name="Battery",
                    severity=sev,
                    value=f"{charge:.0f}%",
                    detail=f"{v.get('battery.voltage', '—')} V",
                    group="Status",
                    metric=charge,
                    metric_unit="%",
                    percent=charge,
                )
            )

        runtime = _f(v.get("battery.runtime"))
        if runtime is not None:
            minutes = runtime / 60
            warn_min = float(self.opt("runtime_warn_min", 10))
            sev = CRITICAL if minutes < warn_min / 2 else WARNING if minutes < warn_min else OK
            panel.checks.append(
                Check(
                    id="ups.runtime",
                    name="Runtime left",
                    severity=sev,
                    value=f"{minutes:.0f} min",
                    detail="estimated at current load",
                    group="Status",
                    metric=round(minutes, 1),
                    metric_unit="min",
                )
            )

        load = _f(v.get("ups.load"))
        if load is not None:
            sev = CRITICAL if load > 90 else WARNING if load > 75 else OK
            panel.checks.append(
                Check(
                    id="ups.load",
                    name="Load",
                    severity=sev,
                    value=f"{load:.0f}%",
                    detail=f"{v.get('ups.realpower.nominal', '—')} W nominal",
                    group="Status",
                    metric=load,
                    metric_unit="%",
                    percent=load,
                )
            )

        panel.summary = ", ".join(words) or "status unknown"
        # Only the fields the UI actually uses. Shipping the whole LIST VAR
        # dump to every browser on every poll is an unbounded third-party
        # blob for no benefit.
        panel.extra = {
            "model": v.get("ups.model") or v.get("device.model", ""),
            "status": v.get("ups.status", ""),
            "input_voltage": v.get("input.voltage", ""),
        }
        return panel


def _f(value: str | None) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
