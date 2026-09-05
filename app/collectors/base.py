"""Base class every collector inherits from."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from ..config import Config
from ..models import Panel
from ..version import __version__

log = logging.getLogger("panel.collector")


def _host_of(exc: httpx.HTTPError) -> str:
    """Host from an httpx error, safely.

    `exc.request` is a property that *raises* when it was never set, so it
    cannot be truth-tested — `exc.request if exc.request else ...` throws
    inside the except block instead of falling back. An empty host (a relative
    URL, e.g. when a ${VAR} expanded to nothing) is also reported honestly
    rather than as a blank.
    """
    try:
        host = exc.request.url.host
    except RuntimeError:
        return "host"
    return host or "an empty host (check the URL in your config)"


class CollectorError(Exception):
    """An expected, explainable failure — a missing tool, an empty API reply,
    a service that answered but not usefully. Reported to the user, logged as
    a single line, never as a traceback."""


class Collector:
    key = "base"
    title = "Base"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.conf: dict[str, Any] = cfg.section(self.key)
        self.interval = cfg.interval(self.key)
        self._clients: dict[tuple[bool, float], httpx.AsyncClient] = {}

    # -- helpers -------------------------------------------------------

    def opt(self, name: str, default: Any = None) -> Any:
        return self.conf.get(name, default)

    def client(self, verify: bool | None = None, timeout: float = 10.0) -> httpx.AsyncClient:
        """Reused client per verify mode so connections stay warm."""
        verify = self.opt("verify_ssl", True) if verify is None else verify
        # The timeout belongs in the cache key. Keying on verify alone means a
        # second call with a different timeout silently reuses the first
        # client and the new timeout is ignored.
        key = (bool(verify), float(timeout))
        if key not in self._clients:
            self._clients[key] = httpx.AsyncClient(
                verify=key[0],
                timeout=timeout,
                follow_redirects=False,
                headers={"User-Agent": f"homelab-panel/{__version__}"},
            )
        return self._clients[key]

    async def aclose(self) -> None:
        for c in self._clients.values():
            try:
                await c.aclose()
            except Exception:  # pragma: no cover - shutdown best effort
                pass
        self._clients.clear()

    # -- lifecycle -----------------------------------------------------

    async def collect(self) -> Panel:
        """Override this. Raise anything; run() turns it into a clean error panel."""
        raise NotImplementedError

    async def run(self) -> Panel:
        started = time.perf_counter()
        try:
            panel = await self.collect()
        except httpx.HTTPStatusError as exc:
            panel = Panel(
                key=self.key,
                title=self.title,
                error=f"{exc.response.status_code} from {_host_of(exc)}",
            )
        except httpx.RequestError as exc:
            panel = Panel(
                key=self.key,
                title=self.title,
                error=f"Cannot reach {_host_of(exc)}",
            )
        except CollectorError as exc:
            log.warning("%s: %s", self.key, exc)
            panel = Panel(key=self.key, title=self.title, error=str(exc)[:200])
        except (TimeoutError, ConnectionError, OSError) as exc:
            # Expected when a box is down. Worth a log line, not a traceback.
            target = self.opt("host", "the target")
            port = self.opt("port")
            where = f"{target}:{port}" if port else str(target)
            log.warning("%s could not reach %s (%s)", self.key, where, type(exc).__name__)
            panel = Panel(key=self.key, title=self.title, error=f"Cannot reach {where}")
        except Exception as exc:  # noqa: BLE001 - a broken collector must not kill the loop
            log.exception("collector %s failed", self.key)
            panel = Panel(key=self.key, title=self.title, error=str(exc)[:200])

        panel.duration_ms = int((time.perf_counter() - started) * 1000)
        panel.ts = time.time()
        return panel.resolve()
