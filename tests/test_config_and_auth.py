"""Config loading, validation robustness, redaction, and the HTTP auth layer."""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.validate import validate

from .conftest import write_config

# ---------- validate() must never raise ----------


@pytest.mark.parametrize(
    "yaml_text, expect_fragment",
    [
        ("config_version: 1\nserver: {host: 0.0.0.0, port: 8080}\n"
         "backups: {enabled: true, bucket: b, key_id: k, app_key: a, targets: [{name: d, prefix: 'x/', max_age_hours: oops}]}\n",
         "max_age_hours must be a finite number"),
        ("config_version: 1\nserver: {host: 0.0.0.0, port: yes}\n", "server.port must be a finite number"),
        ("config_version: 1\nserver: {host: 0.0.0.0, port: 8080}\nfleet: {enabled: true, hosts: ['str', 42]}\n",
         "fleet.hosts[0] must be a mapping"),
        ("config_version: 1\nserver: {host: 0.0.0.0, port: 8080}\nhost_metrics: {enabled: true, interval: .inf}\n",
         "interval must be a finite number"),
        ("config_version: 1\nserver: {host: 0.0.0.0, port: 8080}\n"
         "proxmox: {enabled: true, host: 'https://x', token_id: a, token_secret: b, storage_warn_pct: 95, storage_crit_pct: 80}\n",
         "warnings would never fire"),
        ("config_version: 1\nserver: {host: 0.0.0.0, port: 8080}\nunifi: {enabled: true, host: 'https://x', auth_mode: login}\n",
         "unifi.username is required"),
        ("config_version: 1\nserver: {host: 0.0.0.0, port: 8080}\npihole: {enabled: true, host: 'http://x', api_version: 5}\n",
         "pihole.api_token is required"),
        ("config_version: 1\nserver: {host: 0.0.0.0, port: 8080}\nalerts: yes\n", "alerts must be a mapping"),
        ("config_version: 1\nserver: {host: 0.0.0.0, port: 8080}\nproxmox: {enabled: true, host: '10.0.0.4:8006', token_id: a, token_secret: b}\n",
         "full http:// or https:// URL"),
        ("config_version: 1\nserver: {host: 0.0.0.0, port: 8080}\nproxmox: {enabled: true, host: 'https://[fe80::1', token_id: a, token_secret: b}\n",
         "full http:// or https:// URL"),
        ("config_version: 1\nserver: {host: 0.0.0.0, port: 8080}\nservices: {enabled: true, checks: [{name: [a], url: 'https://x'}]}\n",
         "name must be text"),
        # Round 3: validation must be at least as strict as the runtime's int().
        ("config_version: 1\nserver: {host: 0.0.0.0, port: '8080.0'}\n", "server.port must be a whole number"),
        ("config_version: 1\nserver: {host: 0.0.0.0, port: 8080}\npoll: {default_interval: '3e1'}\n",
         "poll.default_interval must be a whole number"),
        ("config_version: 1\nserver: {host: 0.0.0.0, port: 8080}\nproxmox: true\n", "proxmox must be a mapping"),
        ("config_version: 1\nserver: {host: 0.0.0.0, port: 8080}\nhost_metrics: {enabled: 'false'}\n",
         "host_metrics.enabled must be true or false"),
        ("config_version: 1\nserver: {host: 0.0.0.0, port: 8080}\n"
         "services: {enabled: true, checks: [{name: a, url: 'https://x', expect_status: ok}]}\n",
         "expect_status must be an HTTP status"),
    ],
)
def test_validator_reports_instead_of_raising(yaml_text, expect_fragment):
    errors, _ = validate(Config.load(write_config(yaml_text)))
    assert any(expect_fragment in e for e in errors), errors


def test_shipped_configs_validate(monkeypatch):
    for var in ("PVE_TOKEN_SECRET", "UNIFI_API_KEY", "WAZUH_PASSWORD", "WAZUH_INDEXER_PASSWORD",
                "PIHOLE_PASSWORD", "B2_KEY_ID", "B2_APP_KEY", "CF_ACCOUNT_ID", "CF_API_TOKEN",
                "SPLUNK_PASSWORD", "AUTHENTIK_TOKEN", "JELLYFIN_API_KEY", "GITHUB_TOKEN"):
        monkeypatch.setenv(var, "placeholder")
    monkeypatch.setenv("HEARTBEAT_URL", "https://example.com/ping")
    for name in ("config.example.yaml", "config.starter.yaml"):
        errors, _ = validate(Config.load(name))
        assert not errors, f"{name}: {errors}"


def test_unset_api_token_variable_is_an_error(monkeypatch):
    monkeypatch.delenv("PANEL_TOKEN", raising=False)
    errors, _ = validate(Config.load(write_config(
        "config_version: 1\nserver: {host: 0.0.0.0, port: 8080, api_token: '${PANEL_TOKEN}'}\nhost_metrics: {enabled: true}\n"
    )))
    assert any("authentication disabled" in e for e in errors)


def test_embedded_unset_variable_is_missing_not_truncated(monkeypatch):
    """Round 3, finding 16: "prefix-${UNSET}" must not become the token "prefix-"."""
    monkeypatch.delenv("R3_UNSET_SECRET", raising=False)
    cfg = Config.load(write_config(
        "config_version: 1\nserver: {host: 127.0.0.1, port: 8080, api_token: 'prefix-${R3_UNSET_SECRET}'}\n"
    ))
    assert not cfg.get("server.api_token")
    assert ("server.api_token", "R3_UNSET_SECRET") in cfg.missing_vars()
    errors, _ = validate(cfg)
    assert any("R3_UNSET_SECRET" in e for e in errors), errors


def test_quoted_false_does_not_enable_a_collector():
    cfg = Config.load(write_config(
        "config_version: 1\nserver: {host: 127.0.0.1, port: 8080}\nhost_metrics: {enabled: 'false'}\n"
    ))
    assert not cfg.enabled("host_metrics")


def test_missing_env_reads_as_absent(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    cfg = Config.load(write_config("config_version: 1\nserver: {port: '${NOPE}'}\n"))
    assert cfg.get("server.port", 8080) == 8080
    assert cfg.missing_vars() == [("server.port", "NOPE")]


def test_dotenv_is_loaded_from_beside_config(tmp_path, monkeypatch):
    monkeypatch.delenv("FROM_DOTENV", raising=False)
    (tmp_path / ".env").write_text("FROM_DOTENV=hello\n# comment\nQUOTED='q'\n")
    (tmp_path / "config.yaml").write_text("config_version: 1\nsite: {name: '${FROM_DOTENV}', subtitle: '${QUOTED}'}\n")
    cfg = Config.load(tmp_path / "config.yaml")
    assert cfg.get("site.name") == "hello"
    assert cfg.get("site.subtitle") == "q"
    del os.environ["FROM_DOTENV"]
    del os.environ["QUOTED"]


# ---------- redaction ----------


def test_public_view_redacts_every_canary():
    cfg = Config.load(write_config(
        "config_version: 1\nsite: {name: t}\n"
        "heartbeat: {enabled: true, url: 'https://hc-ping.com/CANARY-ONE'}\n"
        "alerts: {enabled: true, webhook_url: 'https://hooks.example.com/CANARY-TWO'}\n"
        "proxmox: {enabled: true, host: 'https://10.0.0.4:8006', token_secret: CANARY-THREE}\n"
        "services:\n  enabled: true\n  checks:\n    - name: gui\n      url: 'https://user:CANARY-FOUR@example.com/'\n"
    ))
    out = json.dumps(cfg.public_view())
    for canary in ("CANARY-ONE", "CANARY-TWO", "CANARY-THREE", "CANARY-FOUR"):
        assert canary not in out
    assert '"enabled": true' in out  # publishable keys survive


# ---------- HTTP auth ----------


@pytest.fixture
def client(monkeypatch):
    """A TestClient against the real app, with lifespan (engine) running."""
    cfg_path = write_config(
        "config_version: 1\nsite: {name: t}\n"
        "server: {host: 127.0.0.1, port: 8099, api_token: 'good-token'}\n"
        "poll: {default_interval: 5}\nhost_metrics: {enabled: true, interval: 3600}\n"
    )  # one real collector here on purpose: /healthz ready needs a completed poll
    import tempfile

    monkeypatch.setenv("PANEL_CONFIG", str(cfg_path))
    monkeypatch.setenv("PANEL_DB", tempfile.mktemp(suffix=".db"))
    import importlib

    import app.main as main_module
    importlib.reload(main_module)
    with TestClient(main_module.app) as c:
        yield c


def test_unauthenticated_is_rejected(client):
    assert client.get("/api/status").status_code == 401


def test_bearer_header_accepted(client):
    assert client.get("/api/status", headers={"Authorization": "Bearer good-token"}).status_code == 200


def test_new_query_token_beats_stale_cookie(client):
    """Token rotation: an old cookie must not block the correct new token."""
    client.cookies.set("panel_session", "old-token")
    res = client.get("/api/status?token=good-token")
    assert res.status_code == 200


def test_rejected_stale_cookie_is_cleared(client):
    client.cookies.set("panel_session", "old-token")
    res = client.get("/api/status")
    assert res.status_code == 401
    assert "panel_session=;" in res.headers.get("set-cookie", "") or "Max-Age=0" in res.headers.get("set-cookie", "")


def test_session_exchange_via_header_sets_httponly_cookie(client):
    res = client.post("/api/session", headers={"Authorization": "Bearer good-token"})
    assert res.status_code == 200 and res.json()["session"] is True
    cookie = res.headers["set-cookie"]
    assert "HttpOnly" in cookie and "panel_session=good-token" in cookie
    # And the cookie alone now authenticates.
    fresh = TestClient(client.app)
    fresh.cookies.set("panel_session", "good-token")
    with fresh:
        assert fresh.get("/api/status").status_code == 200


def test_inf_mute_rejected_and_api_survives(client):
    h = {"Authorization": "Bearer good-token"}
    assert client.post("/api/mutes/x?hours=inf", headers=h).status_code == 400
    assert client.post("/api/mutes/x?hours=nan", headers=h).status_code == 400
    assert client.get("/api/status", headers=h).status_code == 200


def test_cross_site_post_is_rejected(client):
    res = client.post("/api/mutes/x?hours=1&token=good-token", headers={"Sec-Fetch-Site": "cross-site"})
    assert res.status_code == 403
    res = client.post("/api/mutes/x?hours=1&token=good-token", headers={"Sec-Fetch-Site": "same-site"})
    assert res.status_code == 403, "same-site includes sibling origins and must not pass"


def test_same_origin_post_allowed(client):
    res = client.post("/api/mutes/x?hours=1&token=good-token", headers={"Sec-Fetch-Site": "same-origin"})
    assert res.status_code == 200


def test_mismatched_origin_without_fetch_metadata_is_rejected(client):
    res = client.post("/api/mutes/x?hours=1&token=good-token", headers={"Origin": "https://evil.example"})
    assert res.status_code == 403


def test_bearer_header_satisfies_origin_guard(client):
    res = client.post("/api/mutes/x?hours=1", headers={"Authorization": "Bearer good-token", "Sec-Fetch-Site": "cross-site"})
    assert res.status_code == 200


def test_healthz_reports_readiness_and_version(client):
    import time

    deadline = time.time() + 10
    body = client.get("/healthz").json()
    while not body.get("ready") and time.time() < deadline:
        time.sleep(0.3)
        body = client.get("/healthz").json()
    assert body["ok"] is True and body["ready"] is True and body["version"]


def test_non_ascii_token_is_401_not_500(client):
    assert client.get("/api/status?token=%C3%A9").status_code == 401


def test_forwarded_host_satisfies_origin_check(client):
    res = client.post(
        "/api/mutes/x?hours=1&token=good-token",
        headers={"Origin": "https://panel.example.com", "X-Forwarded-Host": "panel.example.com"},
    )
    assert res.status_code == 200
