"""Jellyfin — who is watching what, and whether the box is transcoding.

Transcoding is the number that matters: direct plays are free, transcodes eat
the VM. Seeing three at once explains a sluggish lab better than a CPU graph.
"""

from __future__ import annotations

from ..models import OK, WARNING, Check, Panel
from .base import Collector


class JellyfinCollector(Collector):
    key = "jellyfin"
    title = "Media"

    @property
    def base(self) -> str:
        return str(self.opt("host", "")).rstrip("/")

    async def _get(self, path: str, params: dict | None = None):
        r = await self.client().get(
            f"{self.base}{path}",
            params=params or {},
            headers={"Authorization": f'MediaBrowser Token="{self.opt("api_key")}"'},
        )
        r.raise_for_status()
        return r.json()

    async def collect(self) -> Panel:
        panel = Panel(key=self.key, title=self.title)
        transcode_warn = int(self.opt("transcode_warn", 2))

        info = await self._get("/System/Info")
        panel.checks.append(
            Check(
                id="jellyfin.server",
                name="Server",
                severity=OK,
                value=str(info.get("Version", "up")),
                detail=(
                    f"{info.get('ServerName', 'jellyfin')} · "
                    f"{info.get('OperatingSystemDisplayName', '')}"
                ).strip(" ·"),
                group="Service",
            )
        )
        if info.get("HasPendingRestart"):
            panel.checks.append(
                Check(
                    id="jellyfin.restart",
                    name="Pending restart",
                    severity=WARNING,
                    value="restart required",
                    detail="an update was applied but the server hasn't restarted",
                    group="Service",
                )
            )

        sessions = await self._get("/Sessions", {"activeWithinSeconds": 300})
        playing = [s for s in sessions if s.get("NowPlayingItem")]
        transcoding = [
            s
            for s in playing
            if str(((s.get("PlayState") or {}).get("PlayMethod")) or "").lower().startswith("transcode")
        ]

        panel.checks.append(
            Check(
                id="jellyfin.streams",
                name="Active streams",
                severity=WARNING if len(transcoding) > transcode_warn else OK,
                value=str(len(playing)),
                detail=(
                    f"{len(transcoding)} transcoding · {len(playing) - len(transcoding)} direct"
                    if playing
                    else "nothing playing"
                ),
                group="Playback",
                metric=float(len(playing)),
            )
        )

        for s in playing:
            item = s.get("NowPlayingItem", {})
            title = item.get("SeriesName") or item.get("Name", "unknown")
            method = ((s.get("PlayState") or {}).get("PlayMethod")) or "unknown"
            is_transcode = str(method).lower().startswith("transcode")
            panel.checks.append(
                Check(
                    id=f"jellyfin.session.{s.get('Id')}",
                    name=str(s.get("UserName", "someone")),
                    # Always OK: the aggregate check above owns the threshold.
                    # Warning per-session made transcode_warn dead config,
                    # because one transcode turned the panel yellow no matter
                    # what the threshold said.
                    severity=OK,
                    value=str(method),
                    detail=(
                        f"{title} · {s.get('Client', '')} on {s.get('DeviceName', '')}"
                        + (" · transcoding" if is_transcode else "")
                    ),
                    group="Playback",
                )
            )

        try:
            counts = await self._get("/Items/Counts")
            panel.checks.append(
                Check(
                    id="jellyfin.library",
                    name="Library",
                    severity=OK,
                    value=f"{counts.get('MovieCount', 0):,} films",
                    detail=(
                        f"{counts.get('SeriesCount', 0):,} series · "
                        f"{counts.get('EpisodeCount', 0):,} episodes"
                    ),
                    group="Library",
                    metric=float(counts.get("EpisodeCount", 0)),
                )
            )
        except Exception:  # noqa: BLE001
            pass

        panel.summary = (
            f"{len(playing)} streaming" if playing else "idle"
        )
        return panel
