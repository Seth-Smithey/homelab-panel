"""Service probes — HTTP and TCP reachability for everything without an API."""

from __future__ import annotations

import asyncio
import time

import httpx

from ..models import CRITICAL, OK, UNKNOWN, WARNING, Check, Panel
from .base import Collector


class ServiceCollector(Collector):
    key = "services"
    title = "Services"

    async def _http(self, check: dict) -> Check:
        name = check.get("name", check.get("url", "?"))
        url = check.get("url", "")
        # `expect_status: 401` (a scalar) is as valid as a list; the validator
        # accepts both and so must the runtime, or a typo becomes a phantom
        # "probe failed" while the real service goes unwatched.
        raw_expect = check.get("expect_status", [200])
        expect = {int(x) for x in (raw_expect if isinstance(raw_expect, list) else [raw_expect])}
        verify = bool(check.get("verify_ssl", True))
        timeout = float(self.opt("timeout", 8))
        started = time.perf_counter()
        try:
            r = await self.client(verify=verify, timeout=timeout).get(url)
            ms = (time.perf_counter() - started) * 1000
            ok = r.status_code in expect
            slow = ms > 2500
            sev = OK if ok and not slow else WARNING if ok else CRITICAL
            return Check(
                id=f"svc.{name}",
                name=name,
                severity=sev,
                value=f"{r.status_code}",
                detail=f"{ms:.0f} ms" + ("" if ok else f" · expected {sorted(expect)}"),
                group=check.get("group", "Services"),
                metric=round(ms, 1),
                metric_unit="ms",
                link=url,
            )
        except httpx.RequestError as exc:
            return Check(
                id=f"svc.{name}",
                name=name,
                severity=CRITICAL,
                value="unreachable",
                detail=type(exc).__name__,
                group=check.get("group", "Services"),
                link=url,
            )

    async def _tcp(self, check: dict) -> Check:
        name = check.get("name", "?")
        host, port = check.get("host", ""), int(check.get("port", 0))
        started = time.perf_counter()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=float(self.opt("timeout", 8))
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            ms = (time.perf_counter() - started) * 1000
            return Check(
                id=f"tcp.{name}",
                name=name,
                severity=OK,
                value="open",
                detail=f"{host}:{port} · {ms:.0f} ms",
                group=check.get("group", "Services"),
                metric=round(ms, 1),
                metric_unit="ms",
            )
        except (TimeoutError, OSError):
            return Check(
                id=f"tcp.{name}",
                name=name,
                severity=CRITICAL,
                value="closed",
                detail=f"{host}:{port} refused or timed out",
                group=check.get("group", "Services"),
            )

    async def collect(self) -> Panel:
        panel = Panel(key=self.key, title=self.title)
        http_checks = [c for c in (self.opt("checks", []) or []) if isinstance(c, dict)]
        tcp_checks = [c for c in (self.opt("tcp_checks", []) or []) if isinstance(c, dict)]
        configured = http_checks + tcp_checks
        tasks = [self._http(c) for c in http_checks]
        tasks += [self._tcp(c) for c in tcp_checks]
        if not tasks:
            panel.error = "No service checks configured"
            return panel

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, Check):
                panel.checks.append(res)
            elif isinstance(res, BaseException):
                # Keep the id tied to the configured check, not to a Python
                # object address: ids that change every poll orphan mutes and
                # history, and "unknown" is the honest state — the probe did
                # not run, so nothing is known about the service.
                entry = configured[len(panel.checks)] if len(panel.checks) < len(configured) else {}
                name = str(entry.get("name") or entry.get("url") or entry.get("host") or "?")
                panel.checks.append(
                    Check(
                        id=f"svc.{name}",
                        name=name,
                        severity=UNKNOWN,
                        value="probe failed",
                        detail=str(res)[:120],
                        group=str(entry.get("group", "Services")),
                    )
                )

        down = sum(1 for c in panel.checks if c.severity == CRITICAL)
        panel.summary = (
            f"{len(panel.checks) - down}/{len(panel.checks)} responding"
            if panel.checks
            else "nothing configured"
        )
        return panel
