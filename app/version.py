"""Single source of truth for the version.

Semantic versioning, and the number here is what a release tag carries:
version 1.2.0 is tagged `v1.2.0`. Everything that needs to state a version —
the UI footer, `/healthz`, the Prometheus `homelab_panel_info` metric, the
update check, and `update.sh` — reads it from here, so there is exactly one
place to bump.

MAJOR  an update needs a manual step. Anything in `config.yaml` that stops
       meaning what it did, a dropped collector, a changed API contract.
       The release notes say what to do; `update.sh` refuses to cross a
       major boundary without `--allow-major`.
MINOR  new collectors, new options, new checks. Safe to take blind: every
       config key added in a minor release has a default that preserves the
       previous behaviour.
PATCH  fixes only.
"""

from __future__ import annotations

__version__ = "1.0.0-rc.1"

# Bumped only when config.yaml gains a key that the operator has to *act* on
# (not merely a new optional default). `--config-diff` compares against this
# to decide whether to say "you may want to look at this".
CONFIG_VERSION = 1

# Bumped by adding a migration to app/migrations.py. Never edit an existing
# migration once released — the whole point is that a database written by an
# older version can be walked forward deterministically.
SCHEMA_VERSION = 5


def version_tuple() -> tuple[int, ...]:
    """Comparable form of the running version."""
    return parse_version(__version__)


def parse_version(text: str) -> tuple[int, ...]:
    """Lenient SemVer parse, comparable with `<`.

    Accepts "v1.2.3", "1.2.3", "1.2.3-rc.1", "1.2.3+build". Returns
    (major, minor, patch, is_final) so that a pre-release orders *before*
    its final release — (1, 0, 0, 0) for "1.0.0-rc.1", (1, 0, 0, 1) for
    "1.0.0". Dropping the suffix outright made a running release candidate
    compare equal to the final it was a candidate for, so the update check
    could never tell you the real release had shipped.

    Anything unparseable is (0, 0, 0, 0), so a malformed upstream tag can
    never make the panel claim it is out of date.
    """
    cleaned = str(text).strip().lstrip("vV").split("+", 1)[0]
    core, _, prerelease = cleaned.partition("-")
    parts = core.split(".")
    if len(parts) != 3:
        return (0, 0, 0, 0)
    out: list[int] = []
    for part in parts:
        if not part.isdigit():
            return (0, 0, 0, 0)
        out.append(int(part))
    out.append(0 if prerelease else 1)
    return tuple(out)


def is_prerelease(text: str) -> bool:
    cleaned = str(text).strip().lstrip("vV").split("+", 1)[0]
    return "-" in cleaned and parse_version(text) != (0, 0, 0, 0)
