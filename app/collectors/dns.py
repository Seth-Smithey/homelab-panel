"""DNS — verifies each resolver still answers the way you expect.

Split-horizon setups break quietly: a deleted A record on the internal
resolver just means local clients start going out to the public record
without telling you.
"""

from __future__ import annotations

import asyncio
import time

import dns.asyncresolver
import dns.exception

from ..models import CRITICAL, OK, UNKNOWN, WARNING, Check, Panel
from .base import Collector


class DnsCollector(Collector):
    key = "dns"
    title = "Name resolution"

    async def _check(self, check: dict) -> Check:
        name = check.get("name", check.get("query", "?"))
        query = check.get("query", "")
        expect = str(check.get("expect", "any"))
        server = str(check.get("resolver", "127.0.0.1"))

        # dnspython's nameservers setter rejects anything that is not an IP
        # literal. A hostname here is an easy mistake to make — every other
        # value in this block is one — and outside the try it would take down
        # the whole panel rather than this one check.
        resolver = dns.asyncresolver.Resolver(configure=False)
        try:
            resolver.nameservers = [server]
        except (ValueError, dns.exception.DNSException) as exc:
            return Check(
                id=f"dns.{name}",
                name=name,
                severity=CRITICAL,
                value="bad resolver",
                detail=f"{server!r} must be an IP address, not a hostname ({exc})",
                group="Resolvers",
            )
        resolver.lifetime = float(check.get("timeout", 5))

        started = time.perf_counter()
        try:
            answer = await resolver.resolve(query, check.get("type", "A"))
            ms = (time.perf_counter() - started) * 1000
            records = sorted(r.to_text() for r in answer)
            if expect == "any" or expect in records:
                sev, value = OK, records[0] if records else "no answer"
            else:
                sev, value = WARNING, records[0] if records else "no answer"
            return Check(
                id=f"dns.{name}",
                name=name,
                severity=sev,
                value=value,
                detail=(
                    f"via {resolver.nameservers[0]} · {ms:.0f} ms"
                    + ("" if sev == OK else f" · expected {expect}")
                ),
                group="Resolvers",
                metric=round(ms, 1),
                metric_unit="ms",
            )
        except dns.exception.DNSException as exc:
            return Check(
                id=f"dns.{name}",
                name=name,
                severity=CRITICAL,
                value="no answer",
                detail=f"{type(exc).__name__} from {resolver.nameservers[0]}",
                group="Resolvers",
            )

    async def collect(self) -> Panel:
        panel = Panel(key=self.key, title=self.title)
        checks = self.opt("checks", [])
        if not checks:
            panel.error = "No DNS checks configured"
            return panel
        # return_exceptions so one bad entry cannot discard the others.
        results = await asyncio.gather(
            *[self._check(c) for c in checks], return_exceptions=True
        )
        for entry, result in zip(checks, results, strict=True):
            if isinstance(result, BaseException):
                label = entry.get("name", entry.get("query", "?"))
                panel.checks.append(
                    Check(
                        id=f"dns.{label}",
                        name=label,
                        severity=UNKNOWN,
                        value="check failed",
                        detail=f"{type(result).__name__}: {str(result)[:100]}",
                        group="Resolvers",
                    )
                )
            else:
                panel.checks.append(result)
        failed = sum(1 for c in panel.checks if c.severity != OK)
        panel.summary = (
            "all resolvers answering as expected"
            if not failed
            else f"{failed} resolver answer{'s' if failed > 1 else ''} unexpected"
        )
        return panel
