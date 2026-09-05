"""WAN identity — your real public IP versus what the world thinks it is.

Dynamic DNS fails silently. The updater dies, the lease renews, and every
external name still resolves to an address that stopped being yours. Nothing
in the lab notices until someone tries to connect from outside.
"""

from __future__ import annotations

import ipaddress

import dns.asyncresolver
import dns.exception

from ..models import CRITICAL, OK, WARNING, Check, Panel
from .base import Collector, CollectorError


def _family(address: str) -> int:
    """4, 6, or 0 if the string is not an IP address."""
    try:
        return ipaddress.ip_address(address).version
    except ValueError:
        return 0


class WanCollector(Collector):
    key = "wan"
    title = "Public address"

    async def _public_ip(self) -> tuple[str, str]:
        """Ask a couple of echo services and prefer agreement over speed."""
        answers: dict[str, list[str]] = {}
        for url in self.opt("ip_sources", []):
            try:
                r = await self.client(verify=True, timeout=8).get(url)
                r.raise_for_status()
                ip = r.text.strip().split("\n")[0].strip()
                # Some endpoints return JSON; take the obvious field.
                if ip.startswith("{"):
                    ip = str(r.json().get("ip", "")).strip()
                if ip:
                    answers.setdefault(ip, []).append(url)
            except Exception:  # noqa: BLE001 - one dead echo service is fine
                continue

        if not answers:
            raise CollectorError(
                "No IP echo service answered — check outbound HTTPS from the panel host"
            )

        # Echo services return whichever family the request went out on, so a
        # host with working IPv6 egress gets an IPv6 answer. Prefer IPv4 when
        # any source returned one: DDNS records are overwhelmingly A records,
        # and comparing a v6 address against them can never match.
        prefer_v4 = self.opt("prefer_ipv4", True)
        v4 = {ip: srcs for ip, srcs in answers.items() if _family(ip) == 4}
        pool = v4 if (prefer_v4 and v4) else answers
        best = max(pool.items(), key=lambda kv: len(kv[1]))
        source = best[1][0].split("//")[-1].split("/")[0]
        return best[0], f"{len(best[1])} of {len(self.opt('ip_sources', []))} sources agree ({source})"

    async def collect(self) -> Panel:
        panel = Panel(key=self.key, title=self.title)

        public_ip, detail = await self._public_ip()
        panel.checks.append(
            Check(
                id="wan.ip",
                name="Public IP",
                severity=OK,
                value=public_ip,
                detail=detail,
                group="Address",
            )
        )

        # Compare against every name that should track it.
        resolver = dns.asyncresolver.Resolver(configure=False)
        resolver.nameservers = list(self.opt("resolvers", ["1.1.1.1", "8.8.8.8"]))
        resolver.lifetime = 6.0

        # Query the record type that matches the address we are comparing
        # against, or an A lookup will never match a v6 public address.
        rrtype = "AAAA" if _family(public_ip) == 6 else "A"

        for name in self.opt("ddns_hostnames", []):
            try:
                answer = await resolver.resolve(str(name), rrtype)
                records = sorted(r.to_text() for r in answer)
            except dns.exception.DNSException as exc:
                panel.checks.append(
                    Check(
                        id=f"wan.ddns.{name}",
                        name=str(name),
                        severity=CRITICAL,
                        value="no answer",
                        detail=f"{type(exc).__name__} from public DNS",
                        group="Dynamic DNS",
                    )
                )
                continue

            if public_ip in records:
                panel.checks.append(
                    Check(
                        id=f"wan.ddns.{name}",
                        name=str(name),
                        severity=OK,
                        value="in sync",
                        detail=f"resolves to {records[0]}",
                        group="Dynamic DNS",
                    )
                )
            else:
                panel.checks.append(
                    Check(
                        id=f"wan.ddns.{name}",
                        name=str(name),
                        severity=WARNING,
                        value="stale record",
                        detail=f"points at {records[0]}, but your IP is {public_ip}",
                        group="Dynamic DNS",
                    )
                )

        drifted = sum(1 for c in panel.checks if c.severity != OK)
        panel.summary = public_ip if not drifted else f"{drifted} record(s) out of date"
        panel.extra = {"public_ip": public_ip}
        return panel
