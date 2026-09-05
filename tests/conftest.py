"""Shared fixtures.

Tests run against the real Engine, Store, Config and validator with SQLite in
a temp file and the webhook replaced by a recorder. No collector is exercised
against a live service here — that is what a homelab is for.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# Tests reference shipped files by name; make that independent of the cwd.
os.chdir(ROOT)

from app.config import Config  # noqa: E402
from app.db import Store  # noqa: E402
from app.models import Check, Panel  # noqa: E402
from app.scheduler import Engine  # noqa: E402

BASE_CONFIG = """
config_version: 1
site: {name: test, timezone: UTC}
server: {host: 127.0.0.1, port: 8080, api_token: ""}
poll: {default_interval: 5, history_retention_days: 14}
alerts: {enabled: true, webhook_url: "https://example.invalid/hook", min_severity: warning, startup_grace: 0}
heartbeat: {enabled: false}
# No live collector: a real host_metrics poll would inject host.* checks whose
# severity depends on the CI runner's disk, making "nothing was alerted"
# assertions flaky. Panels are fed to the engine directly.
host_metrics: {enabled: false}
"""


def tmp_file(suffix: str) -> str:
    """A path inside a fresh private directory — no predictable-name race
    (tempfile.mktemp), and SQLite may create the file itself."""
    return os.path.join(tempfile.mkdtemp(prefix="panel-test-"), f"t{suffix}")


def write_config(text: str = BASE_CONFIG) -> Path:
    path = Path(tmp_file(".yaml"))
    path.write_text(text)
    return path


@pytest.fixture
def config_path() -> Path:
    return write_config()


@pytest.fixture
def config(config_path: Path) -> Config:
    return Config.load(config_path)


@pytest.fixture
def store() -> Store:
    st = Store(tmp_file(".db"))
    yield st
    st.close()


@pytest.fixture(autouse=True)
def _keep_all_restored_state(monkeypatch):
    """Tests feed panels straight into the engine without enabling their
    collectors, so the startup policy of dropping state for disabled
    collectors would erase what they are testing. None = keep everything.
    (test_restored_state_of_disabled_collector_is_dropped restores it.)"""
    from app.scheduler import Engine
    monkeypatch.setattr(Engine, "_active_keys", lambda self: None)


@pytest.fixture
def delivered(monkeypatch):
    """Records every alert batch the engine would have POSTed."""
    calls: list[list[dict]] = []

    async def fake_deliver(self, transitions):
        calls.append(transitions)
        return True, ""

    monkeypatch.setattr(Engine, "_deliver", fake_deliver)
    return calls


def make_panel(key: str, title: str, checks: list[Check], error: str = "") -> Panel:
    return Panel(key=key, title=title, checks=checks, error=error).resolve()


def check(check_id: str, severity: str, name: str | None = None, **kw) -> Check:
    return Check(id=check_id, name=name or check_id, severity=severity, value=kw.pop("value", "x"), **kw)


def ids_in(batches: list[list[dict]]) -> list[str]:
    return [t["id"] for batch in batches for t in batch]
