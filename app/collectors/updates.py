"""Is this panel itself up to date?

The one thing a status board is uniquely bad at noticing is its own age. This
asks GitHub for the newest release tag and compares it to the running build.

Deliberately conservative:
  * Off by default. It is the only collector that talks to the internet about
    you rather than about your lab, and that should be a choice.
  * Never worse than WARNING. Being a version behind is not an outage, and a
    board that goes red because a patch shipped is a board you stop reading.
  * Fails quiet. GitHub rate-limits unauthenticated callers to 60 requests an
    hour per IP; hitting that reports "couldn't check", never "out of date".
  * Polls slowly. The default interval is six hours; releases do not happen
    on a 30-second cadence.
"""

from __future__ import annotations

import time

from ..models import OK, UNKNOWN, WARNING, Check, Panel
from ..version import __version__, parse_version
from .base import Collector, CollectorError

GITHUB_API = "https://api.github.com/repos/{repo}/releases/latest"


class UpdateCollector(Collector):
    key = "updates"
    title = "Panel updates"

    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self._cached: dict | None = None
        self._cached_at = 0.0

    async def _latest(self) -> dict:
        repo = str(self.opt("repo", "Seth-Smithey/homelab-panel")).strip("/ ")
        # Cache hard. Even at the default interval a restart loop could
        # otherwise burn the unauthenticated rate limit in minutes.
        ttl = float(self.opt("cache_seconds", 3600))
        if self._cached and (time.time() - self._cached_at) < ttl:
            return self._cached

        headers = {"Accept": "application/vnd.github+json"}
        token = str(self.opt("github_token", "") or "")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        r = await self.client(verify=True, timeout=10).get(
            GITHUB_API.format(repo=repo), headers=headers
        )
        if r.status_code == 404:
            raise CollectorError(
                f"{repo} has no published releases yet (or the repo name is wrong)"
            )
        if r.status_code == 403 and "rate limit" in r.text.lower():
            raise CollectorError(
                "GitHub rate limit reached — set updates.github_token, or raise the interval"
            )
        r.raise_for_status()
        self._cached = r.json()
        self._cached_at = time.time()
        return self._cached

    async def collect(self) -> Panel:
        panel = Panel(key=self.key, title=self.title)
        running = __version__

        try:
            release = await self._latest()
        except CollectorError as exc:
            panel.checks.append(
                Check(
                    id="panel.version",
                    name="Panel version",
                    severity=UNKNOWN,
                    value=running,
                    detail=str(exc),
                    group="Build",
                )
            )
            panel.summary = f"v{running} · update check unavailable"
            return panel

        latest_tag = str(release.get("tag_name") or "").strip()
        latest = parse_version(latest_tag)
        current = parse_version(running)
        # GitHub's /releases/latest never returns a pre-release, so a running
        # rc compares against the newest *stable* — which is the right
        # question: "has the real thing shipped yet?"

        if latest == (0, 0, 0, 0):
            severity, value, detail = (
                UNKNOWN,
                running,
                f"could not read the upstream tag ({latest_tag or 'empty'})",
            )
        elif latest > current:
            major_jump = latest[0] > current[0]
            severity = WARNING
            value = f"{running} → {latest_tag}"
            detail = (
                "a new release is available"
                + (" · major version, read the release notes before updating" if major_jump else "")
                + f" · published {str(release.get('published_at', ''))[:10]}"
                + " · sudo panelctl update"
            )
        else:
            severity, value, detail = OK, running, "running the newest release"

        panel.checks.append(
            Check(
                id="panel.version",
                name="Panel version",
                severity=severity,
                value=value,
                detail=detail,
                group="Build",
                link=str(release.get("html_url", "")) if latest > current else "",
            )
        )
        panel.summary = detail
        panel.extra = {"running": running, "latest": latest_tag}
        return panel
