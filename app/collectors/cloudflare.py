"""Cloudflare — tunnel health for external publishing."""

from __future__ import annotations

from ..models import CRITICAL, OK, WARNING, Check, Panel
from .base import Collector, CollectorError

API = "https://api.cloudflare.com/client/v4"

_SEV = {"healthy": OK, "degraded": WARNING, "down": CRITICAL, "inactive": WARNING}


class CloudflareCollector(Collector):
    key = "cloudflare"
    title = "Edge"

    async def collect(self) -> Panel:
        panel = Panel(key=self.key, title=self.title)
        account = str(self.opt("account_id", ""))
        headers = {"Authorization": f"Bearer {self.opt('api_token')}"}
        watch = {str(t).lower() for t in self.opt("watch_tunnels", [])}

        # Paginated. A tunnel past the first page would otherwise never be
        # listed — and, having been listed once, look resolved.
        tunnels: list[dict] = []
        for page in range(1, 41):
            r = await self.client(verify=True).get(
                f"{API}/accounts/{account}/cfd_tunnel",
                headers=headers,
                params={"is_deleted": "false", "per_page": 50, "page": page},
            )
            r.raise_for_status()
            body = r.json()
            if not body.get("success", False):
                # The v4 envelope always includes an "errors" key, so a default
                # on .get() never fires — but Cloudflare does return it empty
                # for some token-scope and rate-limit rejections, and errors[0]
                # then raises.
                errors = body.get("errors") or [{}]
                raise CollectorError(
                    errors[0].get("message") or "Cloudflare API rejected the request"
                )
            items = body.get("result") or []
            tunnels.extend(items)
            info = body.get("result_info") or {}
            total_pages = int(info.get("total_pages") or 1)
            if not items or page >= total_pages:
                break

        healthy = 0
        for t in tunnels:
            name = str(t.get("name", ""))
            if watch and name.lower() not in watch:
                continue
            status = str(t.get("status", "unknown")).lower()
            sev = _SEV.get(status, WARNING)
            healthy += int(sev == OK)
            conns = t.get("connections") or []
            colos = sorted({c.get("colo_name", "") for c in conns if c.get("colo_name")})
            panel.checks.append(
                Check(
                    id=f"cf.tunnel.{t.get('id')}",
                    name=f"Tunnel {name}",
                    severity=sev,
                    value=status,
                    detail=(
                        f"{len(conns)} connections"
                        + (f" via {', '.join(colos)}" if colos else "")
                    ),
                    group="Tunnels",
                )
            )

        if not panel.checks:
            panel.error = "No matching tunnels returned"
        panel.summary = f"{healthy}/{len(panel.checks)} tunnels healthy"
        return panel
