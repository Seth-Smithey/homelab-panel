"""Backup freshness — checks the newest object under each B2 prefix.

The question this answers is the one that matters at 2am: if the hypervisor
died right now, how old is the newest restore point for each guest?
"""

from __future__ import annotations

import fnmatch
import logging
import time

from ..models import CRITICAL, OK, UNKNOWN, WARNING, Check, Panel
from .base import Collector, CollectorError
from .util import human_age, human_bytes

AUTH_URL = "https://api.backblazeb2.com/b2api/v2/b2_authorize_account"

log = logging.getLogger("panel.collector.backups")


class BackupCollector(Collector):
    key = "backups"
    title = "Backups"

    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self._auth: dict | None = None
        self._auth_ts = 0.0

    async def _authorize(self) -> dict:
        # B2 tokens are good for 24h; refresh every 6.
        if self._auth and time.time() - self._auth_ts < 21600:
            return self._auth
        r = await self.client(verify=True).get(
            AUTH_URL, auth=(str(self.opt("key_id")), str(self.opt("app_key")))
        )
        r.raise_for_status()
        self._auth = r.json()
        self._auth_ts = time.time()
        return self._auth

    async def _bucket_id(self, auth: dict) -> str:
        allowed = auth.get("allowed", {})
        if allowed.get("bucketId"):
            return allowed["bucketId"]
        r = await self.client(verify=True).post(
            f"{auth['apiUrl']}/b2api/v2/b2_list_buckets",
            headers={"Authorization": auth["authorizationToken"]},
            json={"accountId": auth["accountId"], "bucketName": self.opt("bucket")},
        )
        r.raise_for_status()
        buckets = r.json().get("buckets", [])
        if not buckets:
            raise CollectorError(
                f"Bucket {self.opt('bucket')} not found, or the key can't list it"
            )
        return buckets[0]["bucketId"]

    # B2 caps a listing at 10000 names per call and returns them in
    # lexicographic order, not by upload time. Stopping after one page means
    # that once a prefix grows past the page size, the newest restore points
    # sort past the boundary and become invisible — the collector then reports
    # an ever-growing backup age while backups are running normally, which is
    # the exact inverse of the question it exists to answer.
    MAX_PAGES = 20
    PAGE_SIZE = 1000

    async def _newest(
        self,
        auth: dict,
        bucket_id: str,
        prefix: str,
        patterns: list[str] | None = None,
        min_bytes: int = 0,
    ) -> tuple[dict | None, bool]:
        """(newest matching object, listing_complete).

        "Newest object under the prefix" is not the same as "newest restore
        point". A log file, a manifest, or a zero-byte marker written after
        the archive makes a stale backup look fresh. `pattern` (fnmatch on
        the basename) and `min_bytes` say what a real restore point looks
        like; the defaults match vzdump's archives and a typical rclone copy.
        """
        newest: dict | None = None
        start_name: str | None = None
        client = self.client(verify=True)
        complete = False

        for _ in range(self.MAX_PAGES):
            payload: dict = {
                "bucketId": bucket_id,
                "prefix": prefix,
                "maxFileCount": self.PAGE_SIZE,
            }
            if start_name:
                payload["startFileName"] = start_name
            r = await client.post(
                f"{auth['apiUrl']}/b2api/v2/b2_list_file_names",
                headers={"Authorization": auth["authorizationToken"]},
                json=payload,
            )
            r.raise_for_status()
            body = r.json()

            for f in body.get("files", []):
                if f.get("action") != "upload":
                    continue
                basename = str(f.get("fileName", "")).rsplit("/", 1)[-1]
                if patterns and not any(fnmatch.fnmatch(basename, pat) for pat in patterns):
                    continue
                if int(f.get("contentLength", 0) or 0) < min_bytes:
                    continue
                if newest is None or f.get("uploadTimestamp", 0) > newest.get(
                    "uploadTimestamp", 0
                ):
                    newest = f

            start_name = body.get("nextFileName")
            if not start_name:
                complete = True
                break
        else:
            log.warning(
                "prefix %r has more than %d objects; the listing was cut off",
                prefix,
                self.MAX_PAGES * self.PAGE_SIZE,
            )

        return newest, complete

    async def collect(self) -> Panel:
        panel = Panel(key=self.key, title=self.title)
        auth = await self._authorize()
        bucket_id = await self._bucket_id(auth)
        now = time.time()
        total_bytes = 0

        for target in self.opt("targets", []):
            name = target.get("name", target.get("prefix", "?"))
            max_age = float(target.get("max_age_hours", 26)) * 3600
            escalation = CRITICAL if target.get("severity") == "critical" else WARNING

            pattern = target.get("pattern", self.opt("default_pattern", "*.vma*|*.tar*|*.zst|*.gz|*.lzo"))
            patterns = [pt.strip() for pt in str(pattern).split("|") if pt.strip()] if pattern else []
            min_bytes = int(target.get("min_bytes", self.opt("default_min_bytes", 1024 * 1024)) or 0)
            newest, complete = await self._newest(
                auth, bucket_id, target.get("prefix", ""), patterns, min_bytes
            )
            if not complete:
                # An incomplete enumeration cannot support a definitive age.
                panel.checks.append(
                    Check(
                        id=f"backup.{name}",
                        name=name,
                        severity=UNKNOWN,
                        value="too many objects",
                        detail=(
                            f"{target.get('prefix')} has more than "
                            f"{self.MAX_PAGES * self.PAGE_SIZE} objects; use a tighter prefix"
                        ),
                        group="Restore points",
                    )
                )
                continue
            if not newest:
                panel.checks.append(
                    Check(
                        id=f"backup.{name}",
                        name=name,
                        severity=escalation,
                        value="no backup found",
                        detail=f"nothing matching {pattern} under {target.get('prefix')}",
                        group="Restore points",
                    )
                )
                continue

            age = now - newest.get("uploadTimestamp", 0) / 1000
            size = newest.get("contentLength", 0)
            total_bytes += size
            if age > max_age * 2:
                sev = escalation
            elif age > max_age:
                sev = WARNING
            else:
                sev = OK
            panel.checks.append(
                Check(
                    id=f"backup.{name}",
                    name=name,
                    severity=sev,
                    value=human_age(age),
                    detail=f"{human_bytes(size)} · {newest.get('fileName', '').split('/')[-1]}",
                    group="Restore points",
                    metric=round(age / 3600, 1),
                    metric_unit="h",
                )
            )

        panel.summary = f"{human_bytes(total_bytes)} in newest restore points"
        return panel
