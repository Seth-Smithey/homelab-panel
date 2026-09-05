"""Fleet reachability — ICMP across every box you manage.

A TCP probe tells you a service is listening. A ping tells you the machine is
alive. Those are different questions, and when a service check fails you want
to know immediately which one you're dealing with.
"""

from __future__ import annotations

import asyncio
import re
import shutil

from ..models import CRITICAL, OK, UNKNOWN, WARNING, Check, Panel
from .base import Collector, CollectorError

_RTT = re.compile(r"time[=<]\s*([\d.]+)\s*ms")


async def _ping(host: str, timeout: float) -> float | None:
    """Round-trip time in ms, or None if the host didn't answer.

    Uses the system ping so no raw-socket capability is needed; unprivileged
    ICMP works out of the box on Ubuntu via net.ipv4.ping_group_range.
    """
    binary = shutil.which("ping")
    if not binary:
        raise CollectorError(
            "ping is not installed on the panel host — run "
            "'apt install iputils-ping', or set fleet.method to 'tcp'"
        )

    proc = await asyncio.create_subprocess_exec(
        binary, "-c", "1", "-W", str(int(max(1, timeout))), "-n", host,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout + 2)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    if proc.returncode != 0:
        return None
    match = _RTT.search(stdout.decode(errors="replace"))
    return float(match.group(1)) if match else 0.0


async def _tcp_probe(host: str, port: int, timeout: float) -> float | None:
    """Connect-time in ms. A refused connection still proves the host is up,
    which is exactly what this collector is asking."""
    started = asyncio.get_running_loop().time()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    except ConnectionRefusedError:
        pass  # something answered — the host is alive
    except (TimeoutError, OSError):
        return None
    return (asyncio.get_running_loop().time() - started) * 1000


class FleetCollector(Collector):
    key = "fleet"
    title = "Fleet"

    async def _host(self, entry: dict, method: str) -> Check:
        name = entry.get("name") or entry.get("host", "?")
        host = str(entry.get("host", ""))
        critical = bool(entry.get("critical", False))
        warn_ms = float(entry.get("latency_warn_ms", self.opt("latency_warn_ms", 50)))
        timeout = float(self.opt("timeout", 3))

        if method == "tcp":
            port = int(entry.get("port", self.opt("tcp_port", 22)))
            rtt = await _tcp_probe(host, port, timeout)
            how = f"{host}:{port} tcp"
        else:
            rtt = await _ping(host, timeout)
            how = host

        if rtt is None:
            return Check(
                id=f"fleet.{host}",
                name=str(name),
                severity=CRITICAL if critical else WARNING,
                value="no reply",
                detail=f"{how} did not answer",
                group=entry.get("group", "Hosts"),
            )

        sev = WARNING if rtt > warn_ms else OK
        return Check(
            id=f"fleet.{host}",
            name=str(name),
            severity=sev,
            value=f"{rtt:.1f} ms",
            detail=how,
            group=entry.get("group", "Hosts"),
            metric=round(rtt, 2),
            metric_unit="ms",
        )

    async def collect(self) -> Panel:
        panel = Panel(key=self.key, title=self.title)
        hosts = self.opt("hosts", [])
        if not hosts:
            panel.error = "No fleet hosts configured"
            return panel

        # 'auto' prefers ICMP and falls back to TCP in containers or anywhere
        # else ping isn't available. One decision for the whole run, so the
        # panel doesn't mix units between hosts.
        method = str(self.opt("method", "auto")).lower()
        if method == "auto":
            method = "icmp" if shutil.which("ping") else "tcp"

        results = await asyncio.gather(
            *[self._host(h, method) for h in hosts], return_exceptions=True
        )
        for entry, result in zip(hosts, results, strict=True):
            if isinstance(result, Check):
                panel.checks.append(result)
            elif isinstance(result, CollectorError):
                raise result
            else:
                panel.checks.append(
                    Check(
                        id=f"fleet.{entry.get('host', '?')}",
                        name=str(entry.get("name", entry.get("host", "?"))),
                        severity=UNKNOWN,
                        value="probe failed",
                        detail=str(result)[:120],
                        group=entry.get("group", "Hosts"),
                    )
                )

        down = sum(1 for c in panel.checks if c.value == "no reply")
        panel.summary = f"{len(panel.checks) - down}/{len(panel.checks)} answering ({method})"
        return panel
