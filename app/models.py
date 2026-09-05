"""Shared data shapes.

Everything the panel knows reduces to a list of Checks. A Check is one
answerable question ("is the domain controller running", "how old is the last
off-site backup") with a severity, a human-readable value, and optional detail.
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field
from typing import Any

OK = "ok"
WARNING = "warning"
CRITICAL = "critical"
UNKNOWN = "unknown"

_RANK = {OK: 0, UNKNOWN: 1, WARNING: 2, CRITICAL: 3}


def worst(*severities: str) -> str:
    """Return the most serious of the given severities."""
    if not severities:
        return UNKNOWN
    return max(severities, key=lambda s: _RANK.get(s, 1))


def rank(severity: str) -> int:
    return _RANK.get(severity, 1)


@dataclass
class Check:
    """One monitored fact."""

    id: str
    name: str
    severity: str = UNKNOWN
    value: str = ""
    detail: str = ""
    group: str = "General"
    # Numeric reading for sparklines / gauges, when one makes sense.
    metric: float | None = None
    metric_unit: str = ""
    # 0-100 for anything that should render as a bar.
    percent: float | None = None
    link: str = ""
    # An event-like check: one failed task, one noisy rule. It exists while
    # the event is inside its window and then ages out. Ageing out is not a
    # recovery, so the engine forgets an ephemeral check silently instead of
    # announcing that it resolved.
    ephemeral: bool = False
    # Set at snapshot time from the mute table, never by a collector.
    muted: bool = False
    mute_reason: str = ""
    mute_until: float | None = None
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.sanitize()

    def sanitize(self) -> Check:
        """Make this check safe to serialise and safe to reason about.

        A NaN or infinite `metric` or `percent` survives everything up to the
        moment `json.dumps(allow_nan=False)` sees it — at which point the
        whole /api/status response and every SSE frame raise. Python's
        float() happily produces both from strings an API might return, and
        a configured threshold can be nonsense too. So: a reading that is not
        a finite number is not a reading. It becomes absent, and the check
        says so rather than pretending.
        """
        if self.metric is not None:
            try:
                value = float(self.metric)
            except (TypeError, ValueError):
                value = math.nan
            if not math.isfinite(value):
                self.metric = None
                if self.severity == OK:
                    self.severity = UNKNOWN
                    self.detail = (self.detail + " · " if self.detail else "") + "non-numeric reading"
            else:
                self.metric = value
        if self.percent is not None:
            try:
                pct = float(self.percent)
            except (TypeError, ValueError):
                pct = math.nan
            self.percent = min(100.0, max(0.0, pct)) if math.isfinite(pct) else None
        if self.mute_until is not None:
            try:
                until = float(self.mute_until)
            except (TypeError, ValueError):
                until = math.nan
            self.mute_until = until if math.isfinite(until) else None
        if self.severity not in _RANK:
            self.severity = UNKNOWN
        return self

    def dict(self) -> dict[str, Any]:
        return asdict(self)


def sanitize_extra(value: Any) -> Any:
    """Recursively replace non-finite numbers in a free-form dict with None."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(float(value)) else None
    if isinstance(value, dict):
        return {str(k): sanitize_extra(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_extra(v) for v in value]
    return value


@dataclass
class Panel:
    """A collector's output: a titled group of checks plus free-form extras."""

    key: str
    title: str
    severity: str = UNKNOWN
    summary: str = ""
    checks: list[Check] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    duration_ms: int = 0
    ts: float = field(default_factory=time.time)
    stale: bool = False
    # Check-id prefixes this poll could NOT read: one endpoint failed while
    # the rest of the panel succeeded. The engine keeps the last known checks
    # under these prefixes instead of reading their absence as recovery, and
    # `unread_section` adds a visible placeholder so the gap is on the board.
    unread: list[str] = field(default_factory=list)

    def unread_section(
        self, check_id: str, name: str, group: str, reason: str, *prefixes: str
    ) -> None:
        """Record that part of this panel could not be read this time.

        `prefixes` are the check-id prefixes the section would have produced
        (default: the placeholder's own id). A placeholder UNKNOWN check makes
        the missing coverage visible; the prefixes stop the engine from
        treating the section's previous checks as resolved.
        """
        self.unread.extend(prefixes or (check_id,))
        self.checks.append(
            Check(
                id=check_id,
                name=name,
                severity=UNKNOWN,
                value="not read",
                detail=str(reason)[:160],
                group=group,
            )
        )

    def resolve(self) -> Panel:
        """Roll child severities up into the panel severity, ignoring mutes."""
        for check in self.checks:
            check.sanitize()
        self.extra = sanitize_extra(self.extra) if self.extra else {}
        if self.error:
            self.severity = UNKNOWN
        else:
            live = [c.severity for c in self.checks if not c.muted]
            if live:
                self.severity = worst(*live)
            elif self.checks:
                self.severity = OK  # everything here is deliberately silenced
        return self

    def dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["checks"] = [c.dict() for c in self.checks]
        return data
