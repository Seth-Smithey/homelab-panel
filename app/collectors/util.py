"""Small formatting helpers shared by collectors."""

from __future__ import annotations


def human_bytes(value: float | int | None) -> str:
    if not value:
        return "0 B"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(size) < 1024 or unit == "PB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def human_uptime(seconds: float | int | None) -> str:
    if not seconds:
        return "—"
    s = int(seconds)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes = s // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def human_age(seconds: float | int | None) -> str:
    if seconds is None:
        return "never"
    s = int(seconds)
    if s < 90:
        return f"{s}s ago"
    if s < 5400:
        return f"{s // 60}m ago"
    if s < 172800:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def pct_severity(pct: float, warn: float, crit: float) -> str:
    from ..models import CRITICAL, OK, WARNING

    if pct >= crit:
        return CRITICAL
    if pct >= warn:
        return WARNING
    return OK
