"""Authentik — the IdP everything else authenticates against.

If Authentik's workers stop, logins keep working from cache for a while and
then quietly don't. Worth watching directly rather than by HTTP probe.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from ..models import CRITICAL, OK, UNKNOWN, WARNING, Check, Panel
from .base import Collector


def _is_recent(stamp: object, within_seconds: float) -> bool:
    """True if an ISO-8601 timestamp is within the window. Anything
    unparseable is treated as not recent — the conservative reading."""
    if not stamp:
        return False
    try:
        text = str(stamp).replace("Z", "+00:00")
        when = datetime.fromisoformat(text)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return (datetime.now(UTC) - when).total_seconds() <= within_seconds
    except (TypeError, ValueError):
        return False


class AuthentikCollector(Collector):
    key = "authentik"
    title = "Identity"

    @property
    def base(self) -> str:
        return str(self.opt("host", "")).rstrip("/")

    async def _get(self, path: str):
        r = await self.client().get(
            f"{self.base}/api/v3{path}",
            headers={"Authorization": f"Bearer {self.opt('api_token')}"},
        )
        r.raise_for_status()
        return r.json()

    async def collect(self) -> Panel:
        panel = Panel(key=self.key, title=self.title)

        system = await self._get("/admin/system/")
        runtime = system.get("runtime", {})

        # Authentik 2025.8 moved from Celery to Dramatiq and deleted
        # /admin/workers/; the replacement is /tasks/workers. Try the modern
        # endpoint first and fall back, so both eras of server work — and say
        # which one answered, so a permanent "can't tell" is diagnosable.
        worker_count: int | None = None
        worker_source = ""
        worker_error = ""
        for path in ("/tasks/workers", "/admin/workers/"):
            try:
                worker_data = await self._get(path)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    continue
                worker_error = f"{exc.response.status_code} from {path}"
                break
            except Exception as exc:  # noqa: BLE001
                worker_error = f"{type(exc).__name__} from {path}"
                break
            if isinstance(worker_data, dict):
                if "count" in worker_data:
                    worker_count = int(worker_data.get("count") or 0)
                else:
                    worker_count = len(worker_data.get("results") or [])
            else:
                worker_count = len(worker_data or [])
            worker_source = path
            break

        if worker_count is not None:
            panel.checks.append(
                Check(
                    id="authentik.workers",
                    name="Background workers",
                    severity=OK if worker_count > 0 else CRITICAL,
                    value=f"{worker_count} running",
                    detail=f"workers handle logins, syncs, and outpost updates · {worker_source}",
                    group="Core",
                    metric=float(worker_count),
                )
            )
        else:
            # A panel with no worker check rolls up to OK — showing Identity
            # green at exactly the moment the workers are dead, which is the
            # failure this check exists to catch. So it is always present.
            panel.checks.append(
                Check(
                    id="authentik.workers",
                    name="Background workers",
                    severity=UNKNOWN,
                    value="can't tell",
                    detail=worker_error or "neither /tasks/workers nor /admin/workers/ exists on this server",
                    group="Core",
                )
            )

        version = await self._get("/admin/version/")
        current = str(version.get("version_current", "?"))
        latest = str(version.get("version_latest", current))
        outdated = bool(version.get("outdated", False))
        panel.checks.append(
            Check(
                id="authentik.version",
                name="Version",
                severity=WARNING if outdated else OK,
                value=current,
                detail=f"latest is {latest}" if outdated else "up to date",
                group="Core",
            )
        )

        panel.checks.append(
            Check(
                id="authentik.runtime",
                name="Server",
                severity=OK,
                value="responding",
                detail=(
                    f"python {runtime.get('python_version', '?')} · "
                    f"{system.get('server_time', '')[:19].replace('T', ' ')}"
                ).strip(" ·"),
                group="Core",
            )
        )

        # Outposts are what actually enforce proxy/LDAP providers. Health comes
        # from the dedicated endpoint, and "last_seen" is only evidence of a
        # connection if it is recent — an outpost that checked in last Tuesday
        # is not connected today.
        stale_after = float(self.opt("outpost_stale_seconds", 300))
        try:
            outposts = await self._get("/outposts/instances/")
            for item in outposts.get("results", []):
                name = str(item.get("name", "outpost"))
                pk = item.get("pk")
                health: list[dict] = []
                try:
                    fetched = await self._get(f"/outposts/instances/{pk}/health/")
                    if isinstance(fetched, list):
                        health = fetched
                    elif isinstance(fetched, dict):
                        health = fetched.get("results") or []
                except Exception:  # noqa: BLE001 - older servers embed it
                    health = item.get("health") or []

                fresh = [h for h in health if _is_recent(h.get("last_seen"), stale_after)]
                version_ok = all(not h.get("version_outdated") for h in health) if health else True
                if not health:
                    sev, value = WARNING, "never checked in"
                elif not fresh:
                    sev, value = CRITICAL, "not reporting"
                elif not version_ok:
                    sev, value = WARNING, "version mismatch"
                else:
                    sev, value = OK, "connected"
                panel.checks.append(
                    Check(
                        id=f"authentik.outpost.{pk}",
                        name=f"Outpost {name}",
                        severity=sev,
                        value=value,
                        detail=f"{len(fresh)} of {len(health)} instances seen in the last {int(stale_after)}s",
                        group="Outposts",
                    )
                )
        except Exception as exc:  # noqa: BLE001
            panel.checks.append(
                Check(
                    id="authentik.outposts",
                    name="Outposts",
                    severity=UNKNOWN,
                    value="can't tell",
                    detail=f"outpost listing failed: {type(exc).__name__}",
                    group="Outposts",
                )
            )

        panel.summary = f"authentik {current}"
        return panel
