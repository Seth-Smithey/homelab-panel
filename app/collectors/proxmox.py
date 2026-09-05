"""Proxmox VE — node health, guest state, storage pressure."""

from __future__ import annotations

import logging
import time

from ..models import CRITICAL, OK, UNKNOWN, WARNING, Check, Panel
from .base import Collector
from .util import human_bytes, human_uptime

log = logging.getLogger("panel.collector.proxmox")


class ProxmoxCollector(Collector):
    key = "proxmox"
    title = "Compute"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"PVEAPIToken={self.opt('token_id')}={self.opt('token_secret')}"
        }

    async def _get(self, path: str):
        base = str(self.opt("host", "")).rstrip("/")
        r = await self.client().get(f"{base}/api2/json{path}", headers=self._headers())
        r.raise_for_status()
        return r.json().get("data")

    async def collect(self) -> Panel:
        panel = Panel(key=self.key, title=self.title)
        critical_guests = {str(g).lower() for g in self.opt("critical_guests", [])}
        warn_pct = float(self.opt("storage_warn_pct", 80))
        crit_pct = float(self.opt("storage_crit_pct", 92))

        resources = await self._get("/cluster/resources") or []

        nodes = [r for r in resources if r.get("type") == "node"]
        guests = [r for r in resources if r.get("type") in ("qemu", "lxc")]
        stores = [r for r in resources if r.get("type") == "storage"]

        for node in nodes:
            online = node.get("status") == "online"
            cpu = float(node.get("cpu") or 0) * 100
            mem = float(node.get("mem") or 0)
            maxmem = float(node.get("maxmem") or 1)
            mem_pct = mem / maxmem * 100
            sev = OK if online else CRITICAL
            if online and (cpu > 90 or mem_pct > 92):
                sev = WARNING
            panel.checks.append(
                Check(
                    id=f"pve.node.{node.get('node')}",
                    name=f"Node {node.get('node')}",
                    severity=sev,
                    value="online" if online else "offline",
                    detail=(
                        f"CPU {cpu:.0f}% · RAM {human_bytes(mem)} of {human_bytes(maxmem)}"
                        f" · up {human_uptime(node.get('uptime'))}"
                    ),
                    group="Nodes",
                    metric=round(cpu, 1),
                    metric_unit="%",
                    percent=round(mem_pct, 1),
                )
            )

        running = 0
        for g in sorted(guests, key=lambda x: str(x.get("name") or "")):
            name = str(g.get("name") or g.get("vmid"))
            status = g.get("status", "unknown")
            is_running = status == "running"
            running += int(is_running)
            is_critical = name.lower() in critical_guests
            # A stopped template is not a fault; a stopped guest is, and a
            # stopped guest you named critical is worse.
            if is_running or g.get("template"):
                sev = OK
            elif is_critical:
                sev = CRITICAL
            else:
                sev = WARNING

            mem = float(g.get("mem") or 0)
            maxmem = float(g.get("maxmem") or 1)
            cpu = float(g.get("cpu") or 0) * 100
            detail = (
                f"CPU {cpu:.0f}% · RAM {human_bytes(mem)}/{human_bytes(maxmem)}"
                f" · up {human_uptime(g.get('uptime'))}"
                if is_running
                else f"vmid {g.get('vmid')} on {g.get('node')}"
            )
            panel.checks.append(
                Check(
                    id=f"pve.guest.{g.get('vmid')}",
                    name=name,
                    severity=sev,
                    value=status,
                    detail=detail,
                    group="Guests",
                    metric=round(cpu, 1) if is_running else None,
                    metric_unit="%",
                    percent=round(mem / maxmem * 100, 1) if is_running else None,
                )
            )

        # Deduplicate by (node, storage), not storage alone. Node-local
        # stores legitimately share names across a cluster — every node has
        # its own `local` and `local-lvm` — so keying on the name meant only
        # the first node's copy was ever checked and a full disk on pve2 was
        # invisible. Shared storage reports one row per node with identical
        # figures, so it is collapsed on the pair it actually is shared under.
        seen: set[tuple[str, str]] = set()
        multi_node = len({str(s.get("node", "")) for s in stores if s.get("node")}) > 1
        for s in stores:
            label = str(s.get("storage"))
            node = str(s.get("node", ""))
            shared = bool(s.get("shared"))
            identity = (label, "" if shared else node)
            if identity in seen:
                continue
            seen.add(identity)
            # Only disambiguate in the display when it could be ambiguous.
            display = label if (shared or not multi_node or not node) else f"{label} ({node})"
            check_id = label if shared or not node else f"{label}@{node}"

            # A store that drops out must say so. Skipping it silently removes
            # the check from the panel, and a check that is absent never
            # transitions — so no event is recorded and no alert fires while
            # your backups have nowhere to land.
            if s.get("status") != "available":
                panel.checks.append(
                    Check(
                        id=f"pve.store.{check_id}",
                        name=f"Storage {display}",
                        severity=CRITICAL,
                        value=str(s.get("status") or "unavailable"),
                        detail="storage is not available to the cluster",
                        group="Storage",
                    )
                )
                continue

            used = float(s.get("disk") or 0)
            total = float(s.get("maxdisk") or 0)
            if total <= 0:
                panel.checks.append(
                    Check(
                        id=f"pve.store.{check_id}",
                        name=f"Storage {display}",
                        severity=UNKNOWN,
                        value="no size reported",
                        group="Storage",
                    )
                )
                continue
            pct = used / total * 100
            sev = CRITICAL if pct >= crit_pct else WARNING if pct >= warn_pct else OK
            panel.checks.append(
                Check(
                    id=f"pve.store.{check_id}",
                    name=f"Storage {display}",
                    severity=sev,
                    value=f"{pct:.0f}% used",
                    detail=f"{human_bytes(used)} of {human_bytes(total)}",
                    group="Storage",
                    percent=round(pct, 1),
                )
            )

        await self._add_disk_health(panel, nodes)
        await self._add_failed_tasks(panel, nodes)

        panel.summary = f"{running}/{len(guests)} guests running"
        panel.extra = {"guest_total": len(guests), "guest_running": running}
        return panel

    async def _add_disk_health(self, panel: Panel, nodes: list[dict]) -> None:
        """SMART status per physical disk — the earliest warning you get."""
        if not self.opt("check_disks", True):
            return
        wear_warn = float(self.opt("ssd_wearout_warn_pct", 80))
        for node in nodes:
            name = node.get("node")
            if node.get("status") != "online":
                # An offline node's disks cannot be read; its node check
                # already says so. Keep what was known rather than "recover".
                panel.unread.append(f"pve.disk.{name}.")
                continue
            try:
                disks = await self._get(f"/nodes/{name}/disks/list") or []
            except Exception as exc:  # noqa: BLE001 - needs Sys.Audit
                # Not read is not "all healthy". The previous readings for
                # this node stand, and the gap is on the board.
                panel.unread_section(
                    f"pve.disks.{name}", f"Disks on {name}", "Disks",
                    f"could not read the disk list: {exc} — the token may lack Sys.Audit"
                    " (or set check_disks: false)",
                    f"pve.disk.{name}.",
                )
                continue
            for disk in disks:
                health = str(disk.get("health", "UNKNOWN")).upper()
                dev = str(disk.get("devpath", "disk")).replace("/dev/", "")
                wearout = disk.get("wearout")
                if health in ("PASSED", "OK"):
                    sev = OK
                elif health == "UNKNOWN":
                    sev = UNKNOWN
                else:
                    sev = CRITICAL

                # Proxmox reports wearout as remaining life, not consumed.
                used_pct = None
                if isinstance(wearout, (int, float)) and 0 <= wearout <= 100:
                    used_pct = 100 - float(wearout)
                    if used_pct >= wear_warn and sev == OK:
                        sev = WARNING

                bits = [disk.get("model", ""), human_bytes(disk.get("size"))]
                if used_pct is not None:
                    bits.append(f"{used_pct:.0f}% life used")
                panel.checks.append(
                    Check(
                        id=f"pve.disk.{name}.{dev}",
                        name=f"Disk {dev}",
                        severity=sev,
                        value=health.lower(),
                        detail=" · ".join(b for b in bits if b),
                        group="Disks",
                        percent=round(used_pct, 1) if used_pct is not None else None,
                    )
                )

    async def _add_failed_tasks(self, panel: Panel, nodes: list[dict]) -> None:
        """Recent failed tasks — a backup or migration that died overnight.

        Every failed task in the window becomes its own ephemeral check, so
        one falling out of the window is forgotten, not "recovered", and one
        on the last node is never hidden behind the first few. `pve.tasks`
        carries the total for the headline and the sparkline.
        """
        window = float(self.opt("task_window_hours", 24)) * 3600
        if window <= 0:
            return
        cutoff = time.time() - window
        failures: list[dict] = []
        queried: list[str] = []
        unreachable: list[str] = []
        for node in nodes:
            name = str(node.get("node"))
            if node.get("status") != "online":
                unreachable.append(name)
                panel.unread.append(f"pve.task.UPID:{name}:")
                continue
            try:
                tasks = await self._get(f"/nodes/{name}/tasks?errors=1&limit=200") or []
            except Exception as exc:  # noqa: BLE001
                # Track it. Swallowing the error and moving on turned a
                # permissions failure or a timeout into "nothing failed in
                # the last 24h" — a clean audit that never actually ran. And
                # the tasks this node reported last time are not resolved:
                # UPIDs embed the node name, so the prefix is per node.
                unreachable.append(name)
                panel.unread.append(f"pve.task.UPID:{name}:")
                log.debug("task log on %s not readable: %s", name, exc)
                continue
            queried.append(name)
            for task in tasks:
                status = str(task.get("status", ""))
                if not status or status == "OK":
                    continue
                if float(task.get("starttime", 0)) < cutoff:
                    continue
                failures.append({"node": name, **task})

        hours = int(window / 3600)
        if unreachable and not queried:
            # Nothing was actually checked. Saying "no failures" here is the
            # most misleading thing this collector could do.
            panel.unread_section(
                "pve.tasks", "Recent tasks", "Tasks",
                f"could not read the task log on {', '.join(unreachable)}"
                " — the token may lack Sys.Audit",
            )
            return

        scope = f"in the last {hours}h on {', '.join(queried)}"
        if unreachable:
            scope += f" · could not check {', '.join(unreachable)}"
        panel.checks.append(
            Check(
                id="pve.tasks",
                name="Recent tasks",
                # Partial coverage is not a clean bill of health.
                severity=WARNING if (failures or unreachable) else OK,
                value=(
                    f"{len(failures)} failed" if failures
                    else ("partial check" if unreachable else "no failures")
                ),
                detail=scope,
                group="Tasks",
                metric=float(len(failures)),
            )
        )

        for task in failures:
            when = time.strftime("%d %b %H:%M", time.localtime(task.get("starttime", 0)))
            panel.checks.append(
                Check(
                    id=f"pve.task.{task.get('upid', task.get('starttime'))}",
                    name=f"{task.get('type', 'task')} failed",
                    severity=WARNING,
                    value=str(task.get("status", "error"))[:40],
                    detail=f"{when} on {task.get('node')} · {task.get('id', '')}",
                    group="Tasks",
                    ephemeral=True,
                )
            )
