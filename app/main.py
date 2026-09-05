"""homelab panel — FastAPI application."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import math
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .collectors import REGISTRY
from .config import Config
from .db import MAX_MUTE_HOURS, Store
from .scheduler import Engine
from .validate import validate
from .version import CONFIG_VERSION, SCHEMA_VERSION, __version__

logging.basicConfig(
    # Normalised: logging rejects lowercase level names with a ValueError at
    # import time, and every example .env in this repo writes them lowercase.
    level=os.environ.get("PANEL_LOG_LEVEL", "INFO").strip().upper() or "INFO",
    format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("panel")

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = os.environ.get("PANEL_CONFIG", str(ROOT / "config.yaml"))
DB_PATH = os.environ.get("PANEL_DB", str(ROOT / "data" / "panel.db"))
WEB_DIR = ROOT / "web"

# Session cookie carrying the API token, so the browser stops putting it in
# every URL. Query-string tokens end up in uvicorn's access log, in Caddy's,
# in Cloudflare's, and in browser history — one credential written to four
# places on every poll.
SESSION_COOKIE = "panel_session"

state: dict[str, object] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = Config.load(CONFIG_PATH)

    errors, warnings = validate(cfg)
    for warning in warnings:
        log.warning("config: %s", warning)
    if errors:
        for error in errors:
            log.error("config: %s", error)
        raise SystemExit(
            f"{len(errors)} configuration problem(s). Fix them and restart, "
            f"or run 'sudo panelctl check' to see the list."
        )

    store = Store(DB_PATH)
    if store.schema_from != store.schema_to:
        log.info("database migrated v%d -> v%d", store.schema_from, store.schema_to)
    engine = Engine(cfg, store)
    state.update(cfg=cfg, store=store, engine=engine)
    await engine.start()
    log.info(
        "panel %s ready on %s:%s",
        __version__,
        cfg.get("server.host"),
        cfg.get("server.port"),
    )
    try:
        yield
    finally:
        await engine.stop()
        store.close()


app = FastAPI(title="homelab panel", version=__version__, lifespan=lifespan)


def engine() -> Engine:
    eng = state.get("engine")
    if eng is None:
        raise HTTPException(503, "Engine not started yet")
    return eng  # type: ignore[return-value]


def config() -> Config:
    cfg = state.get("cfg")
    if cfg is None:
        raise HTTPException(503, "Config not loaded yet")
    return cfg  # type: ignore[return-value]


def store() -> Store:
    st = state.get("store")
    if st is None:
        raise HTTPException(503, "Store not ready yet")
    return st  # type: ignore[return-value]


def _expected_token() -> str:
    cfg = state.get("cfg")
    if cfg is None:
        # Fail closed. Returning early here would disable auth on every
        # endpoint during any partial startup.
        raise HTTPException(503, "Config not loaded yet")
    return str(cfg.get("server.api_token", "") or "")  # type: ignore[union-attr]


def _presented_token(request: Request) -> tuple[str, str]:
    """(token, source). Explicit credentials outrank the stored session.

    Order matters for token rotation: after `server.api_token` changes, the
    browser still holds a cookie carrying the *old* token. If the cookie were
    consulted first, opening the board with the correct new `?token=` would
    check the stale cookie, 401, and leave the user stuck until they cleared
    cookies by hand. Header and query string are things the caller chose to
    send right now; the cookie is a memory. Memory loses.
    """
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer ") and header[7:].strip():
        return header[7:].strip(), "header"
    query = request.query_params.get("token", "")
    if query:
        # Kept for EventSource, curl one-liners and the Prometheus scrape
        # config. The browser sends it once, then trades it for a cookie.
        return query, "query"
    cookie = request.cookies.get(SESSION_COOKIE, "")
    if cookie:
        return cookie, "cookie"
    return "", "none"


async def require_token(request: Request) -> None:
    """Bearer auth, only enforced when server.api_token is set."""
    expected = _expected_token()
    if not expected:
        return
    token, source = _presented_token(request)
    # Constant-time: a plain != leaks the length of the matching prefix.
    # Compared as bytes: compare_digest raises TypeError on non-ASCII str, which
    # turned a malformed token into a 500 from inside the auth guard.
    if hmac.compare_digest(token.encode("utf-8", "replace"), expected.encode("utf-8")):
        return
    response_headers = {}
    if source == "cookie" or request.cookies.get(SESSION_COOKIE):
        # A stale session is the most likely reason to be here. Expire it so
        # the next attempt with a fresh token is not blocked by it.
        response_headers["Set-Cookie"] = (
            f"{SESSION_COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"
        )
    raise HTTPException(401, "Invalid or missing token", headers=response_headers or None)


async def require_same_origin(request: Request) -> None:
    """Reject cross-site state-changing requests.

    A POST with no custom header and no body is a CORS-*simple* request, so
    the browser sends it without a preflight and the side effect lands even
    though the attacker cannot read the response. That is enough to mute a
    critical check from any page someone on the LAN happens to open.

    Policy, in order:
      * A bearer token in the Authorization header is proof of a deliberate
        client — a hostile page cannot set that header cross-origin without
        a preflight, which would fail. Allowed.
      * `Sec-Fetch-Site: same-origin` or `none` (typed URL, bookmark) is
        allowed. `same-site` is NOT: it includes every sibling origin under
        the same registrable domain, which is not this app.
      * Without Fetch Metadata, an `Origin` header must match this host
        exactly. Browsers always send Origin on POST.
      * Neither header at all: a non-browser client (curl, a script). Allowed.
    """
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer ") and auth[7:].strip():
        return
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        if site in ("same-origin", "none"):
            return
        raise HTTPException(403, "Cross-site requests are not allowed")
    origin = request.headers.get("origin")
    if origin is None:
        return
    # Behind a reverse proxy the Host header is the proxy's target, not what
    # the browser typed; X-Forwarded-Host carries the original.
    candidates = {
        request.headers.get("host", "").lower(),
        request.headers.get("x-forwarded-host", "").split(",")[0].strip().lower(),
    }
    candidates.discard("")
    if _origin_host(origin) in candidates:
        return
    raise HTTPException(403, "Cross-origin requests are not allowed")


def _origin_host(origin: str) -> str:
    """'https://panel.example.com:8443' -> 'panel.example.com:8443'."""
    text = origin.strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    return text.split("/", 1)[0]


def _clamp(value, default: int, ceiling: int, floor: int = 1) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(floor, min(number, ceiling))


@app.api_route("/healthz", methods=["GET", "HEAD"])
async def healthz() -> JSONResponse:
    """Liveness plus a readiness hint update.sh can act on.

    `ready` means the engine is running and at least one collector has
    completed a poll — a process that answers HTTP but has never collected
    anything is not a working panel. Remote integrations being down does NOT
    make this false: a Proxmox outage must never cause a software rollback.
    """
    eng = state.get("engine")
    polled = len(eng.panels) if eng else 0  # type: ignore[union-attr]
    body = {
        "ok": eng is not None,
        "ready": bool(eng) and (polled > 0 or not eng.collectors),  # type: ignore[union-attr]
        "version": __version__,
        "collectors": len(eng.collectors) if eng else 0,  # type: ignore[union-attr]
        "polled": polled,
    }
    return JSONResponse(body, status_code=200 if body["ok"] else 503)


@app.get("/api/version")
async def version_info() -> JSONResponse:
    """What this build is. Used by update.sh and by the UI footer."""
    st = state.get("store")
    return JSONResponse(
        {
            "version": __version__,
            "config_version": CONFIG_VERSION,
            "schema_version": SCHEMA_VERSION,
            "schema_applied": st.schema_to if st else None,  # type: ignore[union-attr]
        }
    )


@app.post("/api/session", dependencies=[Depends(require_token)])
async def open_session(request: Request) -> JSONResponse:
    """Trade a token for a session cookie.

    The client calls this once with the token in the Authorization header —
    not the query string, so this one request is not logged either — then
    drops the token from every subsequent URL and from its own storage.
    HttpOnly means a future XSS cannot read the cookie back out.

    The cookie is set on the response object that is actually returned — an
    injected `Response` parameter is ignored when the handler returns its own
    response, so setting it there does nothing at all.
    """
    expected = _expected_token()
    if not expected:
        return JSONResponse({"session": False, "reason": "no api_token configured"})
    payload = JSONResponse({"session": True})
    payload.set_cookie(
        SESSION_COOKIE,
        expected,
        httponly=True,
        samesite="strict",
        # A Secure cookie is dropped outright over plain HTTP, which is how
        # most people reach this on a LAN — so only set it when the request
        # actually arrived over TLS.
        secure=request.url.scheme == "https",
        max_age=60 * 60 * 24 * 30,
        path="/",
    )
    return payload


@app.get("/api/status", dependencies=[Depends(require_token)])
async def status() -> JSONResponse:
    return JSONResponse(engine().snapshot())


# Heavier reads run in a worker thread. The Store is guarded by its own lock,
# so this is safe, and it keeps a large history request from stalling every
# collector loop that shares the event loop.


@app.get("/api/events", dependencies=[Depends(require_token)])
async def events(limit: int = 50) -> JSONResponse:
    rows = await asyncio.to_thread(store().events, _clamp(limit, 50, 500))
    return JSONResponse({"events": rows})


@app.get("/api/history/{check_id:path}", dependencies=[Depends(require_token)])
async def history(check_id: str, points: int = 120) -> JSONResponse:
    rows = await asyncio.to_thread(store().history, check_id, _clamp(points, 120, 5000))
    return JSONResponse({"check_id": check_id, "points": rows})


@app.get("/api/sparklines", dependencies=[Depends(require_token)])
async def sparklines(ids: str, points: int = 40) -> JSONResponse:
    wanted = [i for i in ids.split(",") if i][:60]
    data = await asyncio.to_thread(store().sparklines, wanted, _clamp(points, 40, 5000))
    return JSONResponse(data)


@app.get("/api/config", dependencies=[Depends(require_token)])
async def public_config() -> JSONResponse:
    return JSONResponse(config().public_view())


@app.get("/api/stats", dependencies=[Depends(require_token)])
async def db_stats() -> JSONResponse:
    return JSONResponse(await asyncio.to_thread(store().stats))


@app.get("/api/mutes", dependencies=[Depends(require_token)])
async def list_mutes() -> JSONResponse:
    return JSONResponse({"mutes": list(store().active_mutes().values())})


@app.post(
    "/api/mutes/{check_id:path}",
    dependencies=[Depends(require_token), Depends(require_same_origin)],
)
async def add_mute(check_id: str, reason: str = "", hours: float | None = None) -> JSONResponse:
    if hours is not None:
        # inf/nan pass a `<= 0` guard, reach SQLite, and then make every
        # later JSONResponse raise "Out of range float values are not JSON
        # compliant" — a permanent, restart-surviving 500 on the whole API
        # from one request.
        if not math.isfinite(hours) or hours <= 0:
            raise HTTPException(
                400, "hours must be a positive, finite number, or omitted to mute indefinitely"
            )
        if hours > MAX_MUTE_HOURS:
            raise HTTPException(400, f"hours must be at most {MAX_MUTE_HOURS}")
    try:
        return JSONResponse(store().set_mute(check_id, reason, hours))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete(
    "/api/mutes/{check_id:path}",
    dependencies=[Depends(require_token), Depends(require_same_origin)],
)
async def remove_mute(check_id: str) -> JSONResponse:
    if not store().clear_mute(check_id):
        raise HTTPException(404, f"{check_id} was not muted")
    return JSONResponse({"check_id": check_id, "muted": False})


@app.get("/api/availability/{check_id:path}", dependencies=[Depends(require_token)])
async def availability(check_id: str, hours: float = 24) -> JSONResponse:
    if not math.isfinite(hours):
        raise HTTPException(400, "hours must be a finite number")
    current = "unknown"
    for panel in engine().panels.values():
        for check in panel.checks:
            if check.id == check_id:
                current = check.severity
    data = await asyncio.to_thread(store().availability, check_id, max(0.1, hours), current)
    return JSONResponse(data)


@app.get("/metrics", dependencies=[Depends(require_token)])
async def metrics() -> PlainTextResponse:
    """Prometheus exposition. Point Splunk or Prometheus at this if you want
    long-term retention rather than the panel's rolling window."""
    snap = engine().snapshot()
    severity_value = {"ok": 0, "unknown": 1, "warning": 2, "critical": 3}
    lines = [
        "# HELP homelab_panel_info Build information for the running panel.",
        "# TYPE homelab_panel_info gauge",
        f'homelab_panel_info{{version="{_label(__version__)}"}} 1',
        "# HELP homelab_check_severity Severity of one check (0 ok, 1 unknown, 2 warning, 3 critical)."
        " A stale check's OK is exported as unknown; see homelab_check_stale.",
        "# TYPE homelab_check_severity gauge",
    ]
    stale_lines = [
        "# HELP homelab_check_stale 1 when the check's panel has not polled for 3x its interval.",
        "# TYPE homelab_check_stale gauge",
    ]
    readings = [
        "# HELP homelab_check_value Numeric reading attached to a check, where one exists.",
        "# TYPE homelab_check_value gauge",
    ]
    for panel in snap["panels"]:
        for check in panel["checks"]:
            labels = (
                f'panel="{_label(panel["title"])}",check="{_label(check["name"])}",'
                f'id="{_label(check["id"])}",muted="{str(check.get("muted", False)).lower()}"'
            )
            lines.append(
                f"homelab_check_severity{{{labels}}} {severity_value.get(check['severity'], 1)}"
            )
            stale_lines.append(
                f"homelab_check_stale{{{labels}}} {1 if check.get('stale') else 0}"
            )
            value = check.get("metric")
            if value is not None and math.isfinite(float(value)):
                readings.append(f"homelab_check_value{{{labels}}} {value}")
    lines += stale_lines
    lines += readings
    lines += [
        "# HELP homelab_panel_age_seconds Seconds since this panel's last poll attempt, whatever its outcome.",
        "# TYPE homelab_panel_age_seconds gauge",
    ]
    since_success = [
        "# HELP homelab_panel_since_success_seconds Seconds since this panel last polled successfully.",
        "# TYPE homelab_panel_since_success_seconds gauge",
    ]
    stale_panels = [
        "# HELP homelab_panel_stale 1 when the panel has not polled for 3x its interval.",
        "# TYPE homelab_panel_stale gauge",
    ]
    for panel in snap["panels"]:
        plabel = f'panel="{_label(panel["title"])}"'
        age = panel.get("age")
        if age is not None:
            lines.append(f"homelab_panel_age_seconds{{{plabel}}} {age}")
        ok_age = panel.get("last_success")
        if ok_age is not None:
            since_success.append(f"homelab_panel_since_success_seconds{{{plabel}}} {ok_age}")
        stale_panels.append(f"homelab_panel_stale{{{plabel}}} {1 if panel.get('stale') else 0}")
    lines += since_success + stale_panels
    lines.append(f"homelab_overall_severity {severity_value.get(snap['overall'], 1)}")
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


def _label(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


@app.post(
    "/api/refresh/{key}",
    dependencies=[Depends(require_token), Depends(require_same_origin)],
)
async def refresh(key: str) -> JSONResponse:
    panel = await engine().refresh(key)
    if panel is None:
        raise HTTPException(404, f"No collector named {key}")
    return JSONResponse(panel.dict())


@app.get("/api/stream", dependencies=[Depends(require_token)])
async def stream(request: Request) -> StreamingResponse:
    eng = engine()
    # Cap concurrent streams: each one holds a queue and every broadcast fans
    # out to all of them.
    if eng.subscriber_count >= 32:
        raise HTTPException(503, "Too many open streams")

    async def gen():
        # Subscribe *inside* the generator, immediately before sending the
        # first snapshot. Subscribing in the handler body let broadcasts fill
        # the queue during the awaits before Starlette pulls the first frame,
        # so the client received a fresh snapshot and then had up to four
        # older ones replayed over it — the board visibly reverting to a
        # state that had already cleared.
        queue = eng.subscribe()
        try:
            yield f"data: {json.dumps(eng.snapshot(), allow_nan=False)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=20)
                    yield f"data: {json.dumps(payload, allow_nan=False)}\n\n"
                except TimeoutError:
                    # A named event, not an SSE comment: comments never reach
                    # EventSource listeners, so the client could not tell a
                    # healthy-but-quiet stream (every collector on a 60s+
                    # interval) from a dead one. This is transport health
                    # only; freshness of readings is the snapshot's business.
                    beat = json.dumps({"ts": time.time(), "version": __version__})
                    yield f"event: heartbeat\ndata: {beat}\n\n"
        finally:
            eng.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class _NoStoreStatic(StaticFiles):
    """Static files that are always revalidated.

    Browsers apply heuristic freshness to responses without Cache-Control,
    and a reload after a server update then ran the previous release's
    app.js against the new API — with the version toast never reappearing,
    because the page had already seen the new version.
    """

    def file_response(self, *args, **kwargs):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


if WEB_DIR.exists():
    app.mount("/static", _NoStoreStatic(directory=str(WEB_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(WEB_DIR / "index.html"), headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
async def service_worker() -> FileResponse:
    """Served from the root, not /static, so its scope covers the whole app —
    a worker registered under /static/ can only control /static/."""
    return FileResponse(
        str(WEB_DIR / "sw.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/manifest.webmanifest")
async def manifest() -> FileResponse:
    return FileResponse(
        str(WEB_DIR / "manifest.webmanifest"), media_type="application/manifest+json"
    )


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------


def _print_check(cfg_path: str) -> int:
    try:
        cfg = Config.load(cfg_path)
    except Exception as exc:  # noqa: BLE001
        print(f"Config could not be loaded: {exc}")
        return 2

    errors, warnings = validate(cfg)
    for warning in warnings:
        print(f"warning  {warning}")
    for error in errors:
        print(f"error    {error}")
    if errors:
        print(f"\n{len(errors)} problem(s) must be fixed before the panel will start.")
        return 1
    enabled = [c.key for c in REGISTRY if cfg.enabled(c.key)]
    print(f"\nConfig is valid. {len(enabled)} collectors enabled: {', '.join(enabled)}")
    return 0


def _print_config_diff(cfg_path: str) -> int:
    """What the shipped example has that your config.yaml does not.

    This is the update problem nobody warns you about: config.yaml is copied
    from the example once, at install, and then never touched again. Every
    new collector and option added afterwards is silently absent from your
    file. Nothing breaks — every key has a default — but you never find out
    the feature exists. This prints the difference so an update can tell you.
    """
    import yaml

    example_path = ROOT / "config.example.yaml"
    if not example_path.exists():
        print(f"No example config at {example_path}")
        return 2
    try:
        mine = yaml.safe_load(Path(cfg_path).read_text(encoding="utf-8")) or {}
        theirs = yaml.safe_load(example_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"Could not compare: {exc}")
        return 2

    def flatten(node, trail=""):
        out = set()
        if isinstance(node, dict):
            for k, v in node.items():
                path = f"{trail}.{k}" if trail else str(k)
                out.add(path)
                out |= flatten(v, path)
        return out

    added = sorted(flatten(theirs) - flatten(mine))
    removed = sorted(flatten(mine) - flatten(theirs))

    if not added and not removed:
        print(f"config.yaml has every key the v{__version__} example has. Nothing to do.")
        return 0
    if added:
        print(f"New in the v{__version__} example, absent from your config.yaml:")
        print("  (each has a working default — add them only if you want them)\n")
        for key in added:
            print(f"  + {key}")
    if removed:
        print("\nIn your config.yaml but not in the example:")
        print("  (your own additions, or options removed upstream)\n")
        for key in removed:
            print(f"  - {key}")
    print(f"\nReference: {example_path}")
    return 0


def main() -> None:
    import sys

    import uvicorn

    argv = sys.argv[1:]

    if "--version" in argv:
        print(__version__)
        raise SystemExit(0)

    if "--check" in argv:
        raise SystemExit(_print_check(CONFIG_PATH))

    if "--config-diff" in argv:
        raise SystemExit(_print_config_diff(CONFIG_PATH))

    if "--gen-token" in argv:
        print(secrets.token_urlsafe(32))
        raise SystemExit(0)

    cfg = Config.load(CONFIG_PATH)
    try:
        port = int(cfg.get("server.port", 8080) or 8080)
    except (TypeError, ValueError):
        print("server.port is not a number. Fix it in config.yaml.")
        raise SystemExit(2) from None

    uvicorn.run(
        "app.main:app",
        host=str(cfg.get("server.host", "0.0.0.0") or "0.0.0.0"),
        port=port,
        log_level=os.environ.get("PANEL_LOG_LEVEL", "info").strip().lower() or "info",
    )


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        main()
