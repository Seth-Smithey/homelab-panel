"""The panel VM's own vitals — so the dashboard can tell you when it is the problem."""

from __future__ import annotations

import asyncio
import os
import time

import psutil

from ..models import OK, UNKNOWN, WARNING, Check, Panel
from .base import Collector
from .util import human_bytes, human_uptime, pct_severity


class HostMetricsCollector(Collector):
    key = "host_metrics"
    title = "Panel host"

    async def collect(self) -> Panel:
        panel = Panel(key=self.key, title=self.title)
        cpu_warn = float(self.opt("cpu_warn_pct", 85))
        mem_warn = float(self.opt("mem_warn_pct", 88))
        disk_warn = float(self.opt("disk_warn_pct", 85))
        disk_crit = float(self.opt("disk_crit_pct", 93))

        cpu = await asyncio.to_thread(psutil.cpu_percent, 0.4)
        load1, load5, load15 = os.getloadavg()
        cores = psutil.cpu_count() or 1
        panel.checks.append(
            Check(
                id="host.cpu",
                name="CPU",
                severity=OK if cpu < cpu_warn else WARNING,
                value=f"{cpu:.0f}%",
                detail=f"load {load1:.2f} / {load5:.2f} / {load15:.2f} across {cores} cores",
                group="Vitals",
                metric=round(cpu, 1),
                metric_unit="%",
                percent=round(cpu, 1),
            )
        )

        mem = psutil.virtual_memory()
        panel.checks.append(
            Check(
                id="host.memory",
                name="Memory",
                severity=OK if mem.percent < mem_warn else WARNING,
                value=f"{mem.percent:.0f}%",
                detail=f"{human_bytes(mem.used)} of {human_bytes(mem.total)}",
                group="Vitals",
                metric=round(mem.percent, 1),
                metric_unit="%",
                percent=round(mem.percent, 1),
            )
        )

        for path in self.opt("disk_paths", ["/"]):
            try:
                # statvfs blocks uninterruptibly on a hung NFS/CIFS mount, and
                # every collector shares this event loop with the HTTP
                # handlers — calling it inline would freeze the whole dashboard,
                # not just this panel. The timeout bounds the damage to one
                # check; `except OSError` cannot help with a hang.
                usage = await asyncio.wait_for(
                    asyncio.to_thread(psutil.disk_usage, path), timeout=5
                )
            except (TimeoutError, OSError):
                panel.checks.append(
                    Check(
                        id=f"host.disk{path}",
                        name=f"Disk {path}",
                        severity=UNKNOWN,
                        value="unreadable",
                        detail="path is missing, or the mount is not responding",
                        group="Vitals",
                    )
                )
                continue
            panel.checks.append(
                Check(
                    id=f"host.disk{path}",
                    name=f"Disk {path}",
                    severity=pct_severity(usage.percent, disk_warn, disk_crit),
                    value=f"{usage.percent:.0f}%",
                    detail=f"{human_bytes(usage.free)} free of {human_bytes(usage.total)}",
                    group="Vitals",
                    metric=round(usage.percent, 1),
                    metric_unit="%",
                    percent=round(usage.percent, 1),
                )
            )

        uptime = time.time() - psutil.boot_time()
        panel.checks.append(
            Check(
                id="host.uptime",
                name="Uptime",
                severity=OK,
                value=human_uptime(uptime),
                detail=f"since {time.strftime('%d %b %H:%M', time.localtime(psutil.boot_time()))}",
                group="Vitals",
                metric=round(uptime / 86400, 2),
                metric_unit="d",
            )
        )

        panel.summary = f"CPU {cpu:.0f}% · RAM {mem.percent:.0f}%"
        return panel
