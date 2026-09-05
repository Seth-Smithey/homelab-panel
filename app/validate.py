"""Config validation.

Runs at startup and from `python -m app.main --check`. The goal is that a
missing credential or a typo tells you exactly which key to fix, instead of
turning into an unexplained grey panel three hours later.

Every value is checked *before* it is converted, so a typo like
`max_age_hours: oops` becomes a field-specific error rather than a
ValueError traceback out of the validator itself — which is the one place a
crash is least forgivable, since its whole job is to explain problems.
"""

from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import urlparse

from .collectors import REGISTRY
from .config import Config
from .version import CONFIG_VERSION


def _parse_url(value: Any):
    """urlparse that never raises — it throws ValueError on a malformed IPv6
    literal like 'https://[fe80::1', which the validator must report, not
    crash on."""
    if not isinstance(value, str):
        return None
    try:
        return urlparse(value)
    except ValueError:
        return None

# Per collector: settings that must be present when it is enabled.
REQUIRED: dict[str, list[str]] = {
    "proxmox": ["host", "token_id", "token_secret"],
    "unifi": ["host"],
    "wazuh": ["host", "username", "password"],
    "pihole": ["host"],
    "backups": ["bucket", "key_id", "app_key", "targets"],
    "cloudflare": ["account_id", "api_token"],
    "ups": ["host", "ups_name"],
    "splunk": ["host", "username", "password"],
    "authentik": ["host", "api_token"],
    "jellyfin": ["host", "api_key"],
    "services": [],
    "dns": ["checks"],
    "certificates": ["hosts"],
    "host_metrics": [],
    "fleet": ["hosts"],
    "wan": ["ip_sources"],
    "updates": [],
}

URL_FIELDS = {"host"}

# Collectors whose "host" is an HTTP endpoint, not a bare IP (ups is TCP).
SCHEME_REQUIRED = {"proxmox", "unifi", "wazuh", "pihole", "splunk", "authentik", "jellyfin"}

# Conditional credentials: (collector, mode-key, mode-value) -> required keys.
CONDITIONAL: list[tuple[str, str, str, list[str]]] = [
    ("unifi", "auth_mode", "login", ["username", "password"]),
    ("unifi", "auth_mode", "api_key", ["api_key"]),
]

TOP_LEVEL = {"site", "server", "poll", "alerts", "heartbeat", "config_version"}


_INT_TEXT = re.compile(r"^[+-]?[0-9]+$")


def _num(value: Any) -> float | None:
    """A finite number, or None. Booleans are not numbers here — YAML's
    `port: yes` is a mistake, not port 1."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def _is_integer_literal(value: Any) -> bool:
    """Would the runtime's int() accept this exactly as written?

    The validator used to parse with float(), so "8080.0" and "3e1" passed
    and then int("8080.0") raised at startup. Validate with the same
    strictness the runtime applies.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return value.is_integer()
    if isinstance(value, str):
        return bool(_INT_TEXT.match(value.strip()))
    return False


def _check_bool(errors: list[str], path: str, value: Any) -> None:
    """A flag must be a real YAML boolean. A quoted "false" is a non-empty
    string, which is truthy — the collector it was meant to disable ran."""
    if value is not None and not isinstance(value, bool):
        errors.append(
            f"{path} must be true or false (unquoted), got {value!r}"
        )


def _check_number(
    errors: list[str],
    path: str,
    value: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    integer: bool = False,
    required: bool = False,
) -> float | None:
    if value is None:
        if required:
            errors.append(f"{path} is required")
        return None
    number = _num(value)
    if number is None:
        errors.append(f"{path} must be a finite number, got {value!r}")
        return None
    if integer and not _is_integer_literal(value):
        errors.append(f"{path} must be a whole number, got {value!r}")
        return None
    if minimum is not None and number < minimum:
        errors.append(f"{path} must be at least {minimum:g}, got {value!r}")
        return None
    if maximum is not None and number > maximum:
        errors.append(f"{path} must be at most {maximum:g}, got {value!r}")
        return None
    return number


def _check_list_of_dicts(errors: list[str], path: str, value: Any) -> list[dict]:
    if value in (None, [], {}):
        return []
    if not isinstance(value, list):
        errors.append(f"{path} must be a list, got {type(value).__name__}")
        return []
    good: list[dict] = []
    for index, item in enumerate(value):
        if isinstance(item, dict):
            good.append(item)
        else:
            errors.append(f"{path}[{index}] must be a mapping, got {item!r}")
    return good


def validate(cfg: Config) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Errors mean the panel should not start.

    Never raises on bad input. If it does, that is a bug in this file.
    """
    errors: list[str] = []
    warnings: list[str] = []

    for path, var in cfg.missing_vars():
        message = f"{path} references ${{{var}}}, which is not set in the environment or .env"
        if path == "server.api_token":
            errors.append(message + " — the API would start with authentication disabled")
            continue
        # An unset variable inside a section that is switched off, or for a key
        # documented as optional, is not worth a warning on every start.
        section = path.split(".", 1)[0]
        optional = path in ("updates.github_token",) or (
            section in cfg.raw and isinstance(cfg.raw[section], dict)
            and not cfg.raw[section].get("enabled", section in ("site", "server", "poll"))
        )
        if not optional:
            warnings.append(message)

    known = {cls.key for cls in REGISTRY}
    for key in cfg.raw:
        if key in known or key in TOP_LEVEL:
            continue
        warnings.append(f"'{key}' is not a collector the panel knows about — it will be ignored")

    for name in ("site", "server", "poll", "alerts", "heartbeat"):
        raw = cfg.raw.get(name)
        if raw is not None and not isinstance(raw, dict):
            errors.append(f"{name} must be a mapping of settings, got {type(raw).__name__}")

    # Every collector section must be a mapping with a real boolean `enabled`,
    # checked BEFORE the enabled filter: `proxmox: true` used to read as
    # "disabled" and skip validation entirely, and `enabled: "false"` used to
    # enable the collector.
    for cls in REGISTRY:
        raw = cfg.raw.get(cls.key)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            errors.append(
                f"{cls.key} must be a mapping of settings (enabled: true, host: ...), "
                f"got {type(raw).__name__} {raw!r}"
            )
            continue
        _check_bool(errors, f"{cls.key}.enabled", raw.get("enabled"))
    _check_bool(errors, "alerts.enabled", cfg.get("alerts.enabled"))
    _check_bool(errors, "heartbeat.enabled", cfg.get("heartbeat.enabled"))

    _check_number(errors, "server.port", cfg.get("server.port"), minimum=1, maximum=65535, integer=True)

    if not cfg.get("server.api_token"):
        where = str(cfg.get("server.host", ""))
        scope = "all interfaces" if where in ("0.0.0.0", "::") else where or "the default interface"
        warnings.append(
            f"server.api_token is empty, so the API is unauthenticated on {scope} — "
            "fine behind Cloudflare Access or on a trusted LAN, worth setting otherwise "
            "(generate one with: sudo panelctl gen-token)"
        )

    # Intervals are whole seconds: the runtime applies int() to them.
    _check_number(errors, "poll.default_interval", cfg.get("poll.default_interval"), minimum=5, integer=True)
    _check_number(errors, "poll.history_retention_days", cfg.get("poll.history_retention_days"), minimum=1, maximum=3650, integer=True)
    _check_number(errors, "alerts.startup_grace", cfg.get("alerts.startup_grace"), minimum=0)
    _check_number(errors, "heartbeat.interval", cfg.get("heartbeat.interval"), minimum=30, integer=True)

    min_sev = cfg.get("alerts.min_severity")
    if min_sev is not None and str(min_sev) not in ("ok", "unknown", "warning", "critical"):
        errors.append(f"alerts.min_severity must be one of ok, unknown, warning, critical — got {min_sev!r}")

    default_interval = _num(cfg.get("poll.default_interval")) or 30.0

    for cls in REGISTRY:
        key = cls.key
        if not cfg.enabled(key):
            continue
        section = cfg.section(key)
        if not section:
            errors.append(f"{key} is enabled but is not a mapping of settings")
            continue

        for field in REQUIRED.get(key, []):
            value = section.get(field)
            if value in (None, "", [], {}):
                errors.append(
                    f"{key}.{field} is required when {key} is enabled "
                    f"(check your .env if it uses a ${{VAR}} reference)"
                )
                continue
            if field in URL_FIELDS and key in SCHEME_REQUIRED:
                parsed = _parse_url(value)
                if parsed is None or parsed.scheme not in ("http", "https") or not parsed.netloc:
                    errors.append(f"{key}.{field} must be a full http:// or https:// URL, got {value!r}")

        for collector, mode_key, mode_value, needs in CONDITIONAL:
            if key == collector and str(section.get(mode_key, "api_key")) == mode_value:
                for field in needs:
                    if section.get(field) in (None, "", [], {}):
                        errors.append(f"{key}.{field} is required when {key}.{mode_key} is {mode_value!r}")

        interval = section.get("interval", default_interval)
        _check_number(errors, f"{key}.interval", interval, minimum=5, integer=True)
        _check_bool(errors, f"{key}.verify_ssl", section.get("verify_ssl"))
        if "timeout" in section:
            _check_number(errors, f"{key}.timeout", section.get("timeout"), minimum=0.5)

        if section.get("verify_ssl") is False and str(section.get("host", "")).startswith("https"):
            warnings.append(f"{key} talks HTTPS with certificate checks off (expected for self-signed)")

        # Threshold pairs: warn must not exceed crit, or the warning never fires.
        for warn_key, crit_key in (
            ("storage_warn_pct", "storage_crit_pct"),
            ("disk_warn_pct", "disk_crit_pct"),
            ("license_warn_pct", "license_crit_pct"),
            ("index_warn_pct", "index_crit_pct"),
            ("warn_count", "crit_count"),
        ):
            warn = _check_number(errors, f"{key}.{warn_key}", section.get(warn_key), minimum=0)
            crit = _check_number(errors, f"{key}.{crit_key}", section.get(crit_key), minimum=0)
            if warn is not None and crit is not None and warn > crit:
                errors.append(f"{key}.{warn_key} ({warn:g}) is higher than {key}.{crit_key} ({crit:g}) — warnings would never fire")

    if cfg.enabled("backups"):
        for index, target in enumerate(_check_list_of_dicts(errors, "backups.targets", cfg.get("backups.targets"))):
            label = target.get("name") or target.get("prefix") or f"#{index}"
            if not target.get("prefix"):
                errors.append(f"backups target {label} has no prefix")
            _check_number(errors, f"backups.targets[{label}].max_age_hours", target.get("max_age_hours", 26), minimum=0.1)
            if target.get("min_bytes") is not None:
                _check_number(errors, f"backups.targets[{label}].min_bytes", target.get("min_bytes"), minimum=0)

    if cfg.enabled("services"):
        seen: set[str] = set()
        http = _check_list_of_dicts(errors, "services.checks", cfg.get("services.checks"))
        tcp = _check_list_of_dicts(errors, "services.tcp_checks", cfg.get("services.tcp_checks"))
        for check in http + tcp:
            name = check.get("name")
            if name in (None, "", [], {}):
                errors.append("every entry under services needs a name")
            elif not isinstance(name, (str, int, float)):
                errors.append(f"a services check name must be text, got {name!r}")
            elif str(name) in seen:
                errors.append(f"two service checks are both named {name!r} — names must be unique")
            else:
                seen.add(str(name))
        for check in http:
            url = check.get("url")
            parsed = _parse_url(url)
            if parsed is None or parsed.scheme not in ("http", "https"):
                errors.append(f"services check {check.get('name', '?')} needs an http(s) url, got {url!r}")
            expect = check.get("expect_status")
            if expect is not None:
                codes = expect if isinstance(expect, list) else [expect]
                if not codes or not all(_is_integer_literal(c) and 100 <= int(float(c)) <= 599 for c in codes):
                    errors.append(
                        f"services check {check.get('name', '?')}.expect_status must be an HTTP status"
                        f" code or a list of them, got {expect!r}"
                    )
            _check_bool(errors, f"services.checks[{check.get('name', '?')}].verify_ssl", check.get("verify_ssl"))
        for check in tcp:
            if not check.get("host"):
                errors.append(f"services tcp check {check.get('name', '?')} needs a host")
            _check_number(errors, f"services.tcp_checks[{check.get('name', '?')}].port", check.get("port"), minimum=1, maximum=65535, integer=True, required=True)

    if cfg.enabled("fleet"):
        for index, entry in enumerate(_check_list_of_dicts(errors, "fleet.hosts", cfg.get("fleet.hosts"))):
            if not entry.get("host"):
                errors.append(f"fleet.hosts[{index}] needs a 'host' address, got {entry!r}")
            if "method" in entry:
                warnings.append(
                    f"fleet.hosts[{index}].method is ignored — the method is chosen once for the whole run; set fleet.method"
                )
        method = cfg.get("fleet.method", "auto")
        if str(method) not in ("auto", "icmp", "tcp"):
            errors.append(f"fleet.method must be auto, icmp or tcp, got {method!r}")

    if cfg.enabled("dns"):
        for index, entry in enumerate(_check_list_of_dicts(errors, "dns.checks", cfg.get("dns.checks"))):
            if not entry.get("query"):
                errors.append(f"dns.checks[{index}] needs a 'query' name")
            _check_number(errors, f"dns.checks[{index}].timeout", entry.get("timeout"), minimum=0.5)

    if cfg.enabled("certificates"):
        hosts = cfg.get("certificates.hosts")
        if not isinstance(hosts, list) or not all(isinstance(h, str) and h for h in hosts):
            errors.append("certificates.hosts must be a list of 'hostname' or 'hostname:port' strings")

    if cfg.enabled("pihole"):
        version = cfg.get("pihole.api_version", 6)
        if str(version) not in ("5", "6"):
            errors.append(f"pihole.api_version must be 5 or 6, got {version!r}")
        elif str(version) == "5" and not cfg.get("pihole.api_token"):
            errors.append("pihole.api_token is required when pihole.api_version is 5")
        elif str(version) == "6" and not cfg.get("pihole.password"):
            errors.append("pihole.password is required when pihole.api_version is 6")

    if cfg.enabled("wazuh"):
        indexer = cfg.get("wazuh.indexer") or {}
        if not isinstance(indexer, dict):
            errors.append("wazuh.indexer must be a mapping")
        elif indexer.get("enabled"):
            for field in ("host", "username", "password"):
                if not indexer.get(field):
                    errors.append(f"wazuh.indexer.{field} is required when the indexer is enabled")
            warn = _check_number(errors, "wazuh.indexer.warn_count", indexer.get("warn_count", 1), minimum=0)
            crit = _check_number(errors, "wazuh.indexer.crit_count", indexer.get("crit_count", 10), minimum=0)
            if warn is not None and crit is not None and warn > crit:
                errors.append("wazuh.indexer.warn_count is higher than crit_count — warnings would never fire")

    if cfg.get("heartbeat.enabled") and not cfg.get("heartbeat.url"):
        errors.append("heartbeat.enabled is true but heartbeat.url is empty")
    if cfg.get("alerts.enabled") and not cfg.get("alerts.webhook_url"):
        errors.append("alerts.enabled is true but alerts.webhook_url is empty")
    for url_key in ("heartbeat.url", "alerts.webhook_url"):
        url = cfg.get(url_key)
        if url:
            parsed = _parse_url(url)
            if parsed is None or parsed.scheme not in ("http", "https"):
                errors.append(f"{url_key} must be an http(s) URL")

    declared = cfg.get("config_version")
    if declared is None:
        warnings.append(
            f"config.yaml has no config_version; this build expects {CONFIG_VERSION}. "
            "Run 'sudo panelctl config-diff' to see what the current example adds."
        )
    else:
        version = _check_number(errors, "config_version", declared, minimum=1, integer=True)
        if version is not None:
            if version < CONFIG_VERSION:
                warnings.append(
                    f"config.yaml declares config_version {int(version)} but this build is at "
                    f"{CONFIG_VERSION} — run 'sudo panelctl config-diff' to see what changed"
                )
            elif version > CONFIG_VERSION:
                errors.append(
                    f"config.yaml declares config_version {int(version)}, newer than this build "
                    f"understands ({CONFIG_VERSION}). You are running an older panel against a newer config."
                )

    enabled = [cls.key for cls in REGISTRY if cfg.enabled(cls.key)]
    if not enabled:
        warnings.append("no collectors are enabled — the board will be empty")

    return errors, warnings
