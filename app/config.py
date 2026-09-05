"""Configuration loading for the homelab panel.

Reads config.yaml, expands ${ENV_VAR} references, and hands back a plain
dict wrapper with dotted lookups so collectors can do cfg.get("proxmox.host").
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("panel.config")

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Redaction is an allow-list, not a deny-list. A deny-list of key names has
# to be updated every time a config key is added, and it already missed
# `heartbeat.url` — a push URL whose path *is* the credential, so leaking it
# lets anyone keep your dead-man's-switch green while the panel is dead.
# Anything not named here is redacted, so a new secret is safe by default and
# the failure mode of forgetting to update this list is a hidden value rather
# than a leaked one.
PUBLISHABLE_KEYS = {
    # identity / presentation
    "name", "subtitle", "timezone", "title", "group", "label",
    # topology, useful for debugging a config and not itself a secret
    "enabled", "interval", "site", "port", "verify_ssl", "auth_mode", "method",
    "type", "query", "expect", "expect_status", "timeout", "path", "scheme",
    # thresholds
    "warn", "crit", "critical", "severity", "min_severity", "startup_grace",
    "cpu_warn_pct", "mem_warn_pct", "disk_warn_pct", "disk_crit_pct",
    "storage_warn_pct", "storage_crit_pct", "ssd_wearout_warn_pct",
    "license_warn_pct", "license_crit_pct", "index_warn_pct", "index_crit_pct",
    "battery_warn_pct", "runtime_warn_min", "wan_latency_warn_ms",
    "transcode_warn", "warn_days", "crit_days", "warn_count", "crit_count",
    "max_age_hours", "notable_level", "window_minutes", "task_window_hours",
    "default_interval", "history_retention_days",
    "min_active_agents", "check_disks", "prefer_ipv4", "api_version", "cache_seconds",
    "repo", "default_pattern", "default_min_bytes", "ups_name", "watch_tunnels",
    "latency_warn_ms", "outpost_stale_seconds", "ip_sources", "bucket",
    # lists of things being watched (names, not credentials)
    "critical_guests", "watch_devices", "watch_indexes", "disk_paths",
    "targets", "checks", "tcp_checks", "hosts", "resolvers", "ddns_hostnames",
    "tcp_port", "config_version",
}

REDACTED = "***"


def load_dotenv(path: str | os.PathLike[str], override: bool = False) -> int:
    """Load KEY=VALUE lines into os.environ. Returns how many were set.

    systemd hands the service its `.env` via EnvironmentFile, but nothing
    else does — so `python -m app.main --check`, run by hand exactly as the
    README and the installer print it, saw none of the credentials and
    reported every one of them missing against a correctly populated file.
    Loading it here makes the documented command tell the truth.

    Deliberately minimal: no export keyword, no interpolation, no multi-line
    values. It parses what systemd's EnvironmentFile parses, so the two can
    never disagree about what your .env means.
    """
    p = Path(path)
    if not p.is_file():
        return 0
    count = 0
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value
            count += 1
    return count


class MissingEnv(str):
    """A ${VAR} that had no value.

    Substituting a bare "" made an unset variable indistinguishable from a
    deliberate empty string, which is how `server.api_token: "${PANEL_TOKEN}"`
    with the variable unset silently disabled authentication on the entire
    API. This is falsy and compares equal to "" so existing checks behave,
    but it carries the variable name so validate.py can say which one.
    """

    __slots__ = ("var",)

    def __new__(cls, var: str) -> MissingEnv:
        obj = super().__new__(cls, "")
        obj.var = var  # type: ignore[attr-defined]
        return obj


def _expand(value: Any) -> Any:
    """Recursively replace ${VAR} with the environment value.

    Any unresolved reference — alone or embedded in a longer string — makes
    the whole value a MissingEnv. "prefix-${TOKEN}" with TOKEN unset used to
    become the predictable string "prefix-" and pass validation as a token.
    """
    if isinstance(value, str):
        missing = [m.group(1) for m in _ENV_PATTERN.finditer(value) if not os.environ.get(m.group(1))]
        if missing:
            return MissingEnv(missing[0])
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


class Config:
    def __init__(self, data: dict[str, Any], path: Path | None = None) -> None:
        self._data = data
        self.path = path

    @classmethod
    def load(cls, path: str | os.PathLike[str], env_file: str | os.PathLike[str] | None = None):
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(
                f"No config at {p}. Copy config.example.yaml to config.yaml and edit it."
            )
        # A .env sitting beside config.yaml is loaded automatically, which is
        # exactly the layout install.sh creates.
        candidate = Path(env_file) if env_file else p.parent / ".env"
        loaded = load_dotenv(candidate)
        if loaded:
            log.debug("loaded %d variables from %s", loaded, candidate)

        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"{p} is not valid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"{p} must contain a YAML mapping at the top level")
        return cls(_expand(raw), p)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        # An unresolved ${VAR} is absent, not an empty string, so callers'
        # defaults actually apply instead of being overridden by "".
        if isinstance(node, MissingEnv):
            return default
        return node

    def missing_vars(self) -> list[tuple[str, str]]:
        """(dotted.path, VAR_NAME) for every ${VAR} that had no value."""
        found: list[tuple[str, str]] = []

        def walk(node: Any, trail: str) -> None:
            if isinstance(node, MissingEnv):
                found.append((trail, node.var))
            elif isinstance(node, dict):
                for k, v in node.items():
                    walk(v, f"{trail}.{k}" if trail else str(k))
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, f"{trail}[{i}]")

        walk(self._data, "")
        return found

    def section(self, name: str) -> dict[str, Any]:
        value = self._data.get(name)
        return value if isinstance(value, dict) else {}

    def enabled(self, name: str) -> bool:
        """Only a real boolean true (or an unambiguous text form) enables a
        collector. bool("false") is True; the validator rejects non-booleans,
        and the runtime must agree with it rather than run the collector."""
        value = self.section(name).get("enabled", False)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "yes", "on", "1")
        return bool(value) if isinstance(value, (int, float)) else False

    def interval(self, name: str) -> int:
        default = int(self.get("poll.default_interval", 30) or 30)
        raw = self.section(name).get("interval", default)
        try:
            return max(5, int(raw))
        except (TypeError, ValueError):
            return default

    @property
    def raw(self) -> dict[str, Any]:
        return self._data

    def public_view(self) -> dict[str, Any]:
        """Config with everything not explicitly publishable redacted."""

        def scrub(node: Any, key: str | None = None) -> Any:
            if isinstance(node, dict):
                return {k: scrub(v, k) for k, v in node.items()}
            if isinstance(node, list):
                return [scrub(v, key) for v in node]
            if node in (None, "", [], {}):
                return node
            if key is not None and key not in PUBLISHABLE_KEYS:
                return REDACTED
            # A publishable key can still hold a URL with credentials in it.
            text = str(node)
            if "://" in text and "@" in text.split("://", 1)[1].split("/")[0]:
                return REDACTED
            return node

        return {k: scrub(v, k) for k, v in self._data.items()}
