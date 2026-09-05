# homelab panel

One board that answers a single question fast: is anything wrong right now?

![CI](https://github.com/Seth-Smithey/homelab-panel/actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

Everything the panel knows reduces to a **check** — one answerable fact with a
severity. Checks roll up into panels, panels roll up into the headline. If the
headline is green and nothing is muted, the lab is fine.

---

## What it watches

| Panel | Source | What it tells you |
|---|---|---|
| Compute | Proxmox API | Node health, every VM/LXC, storage pressure, SMART disk health, failed tasks |
| Network | UniFi (UDM-Pro) | WAN up/latency/throughput, AP and switch state, client counts |
| Services | HTTP + TCP probes | Anything without an API: web UIs, TCP ports, LDAP/Kerberos |
| Backups | Backblaze B2 | Age of the newest restore point per protected VM |
| Endpoint security | Wazuh API | Agent enrollment, and names the agents not reporting |
| Splunk | Splunk REST (8089) | Server health tree, license headroom, index fullness |
| Power | NUT (upsd) | Mains state, battery charge, runtime remaining, load |
| Identity | Authentik API | Workers, version currency, outpost connectivity |
| DNS filtering | Pi-hole (v5 or v6) | Blocking on/off, query volume, block rate |
| Name resolution | Direct DNS queries | Whether split-horizon still resolves the way you intended |
| Edge | Cloudflare API | Tunnel health and connection count |
| Certificates | TLS handshake | Days remaining on published hostnames |
| Media | Jellyfin API | Active streams and how many are transcoding |
| Fleet | ICMP or TCP | Whether each managed box is alive, and how fast it answers |
| Public address | IP echo + public DNS | Your real WAN IP, and whether DDNS still points at it |
| Panel host | psutil | The dashboard VM's own CPU, memory, disk, uptime |
| Panel updates | GitHub releases | Whether a newer version of the panel exists (off by default) |

Disabled collectors are skipped entirely. Nothing is required.

---

## Install

Ubuntu 22.04 or 24.04, on any VM. It is light — 1 vCPU and 1 GB is plenty.

```bash
git clone https://github.com/Seth-Smithey/homelab-panel.git
cd homelab-panel
sudo bash install.sh
```

This repository *is* the dashboard: `app/`, `web/`, `install.sh` and the
workflows sit at its root, and `update.sh` reads releases from this
repository's tags. If you keep the panel inside a larger repo instead, run
`install.sh` from the subdirectory — it handles that — but the GitHub
workflows and the `updates` collector only work against a repository whose
root is this directory.

**A fresh install comes up nearly empty, on purpose.** It starts from
`config.starter.yaml` — host metrics only, no credentials — so the board is
live and green before any integration is involved. When you add Proxmox and
something doesn't work, you already know the install itself is fine. Then:

```bash
sudo nano /opt/homelab-panel/.env          # credentials
sudo nano /opt/homelab-panel/config.yaml   # copy blocks from current/config.example.yaml
sudo panelctl check                        # validates config + .env against the installed release
sudo panelctl restart
```

`panelctl` is installed to `/usr/local/bin`, so `sudo panelctl …` works from
anywhere. It runs the *installed* release from its own directory,
as the service user, with the service's environment — so `check` cannot
import the wrong code or miss `.env`, from whatever directory you happen to be
in. Add two or three collectors at a time and check between each.

The installer validates the new release against your config **before**
anything running is touched. If the config is wrong it stops and leaves the
current release serving; on a first install it leaves nothing running rather
than enabling a service that crash-loops.

**How it lays things out.** Code is immutable per release; your state lives
beside it and is never inside anything the installer synchronises:

```
/opt/homelab-panel/
  current -> releases/<commit>/     the running release (a symlink)
  releases/<commit>/                code + its own venv, root-owned, never edited
  config.yaml  .env  data/          yours; a release never touches them
  manifest.json                     what is deployed: version, commit, schema
  panelctl                          the wrapper every documented command uses
/var/backups/homelab-panel/         update.sh's recovery points
```

**Installing onto a host that already runs something?** The preflight checks
the port — exempting only this service's own process, by PID — and shows you
what else is listening if there is a conflict. To use a different port:

```bash
sudo PANEL_PORT=8090 ./install.sh
```

That writes `server.port` into `config.yaml`, so the port it checked is the
port the panel binds.

The service runs as its own `panel` user under a hardened systemd unit
(`ProtectSystem=strict`, `ProtectProc=invisible`, no capabilities, native
syscall ABI only). It can write to its own `data/` directory and nowhere else.

---

## Updating

```bash
sudo panelctl update
```

That finds your source checkout from the deployment manifest and runs its
`update.sh`. (`sudo bash update.sh` from the clone is the same thing.)

What it does, in order — and every step before the switch leaves the running
release untouched:

1. **Reads what is deployed** from `/opt/homelab-panel/manifest.json`, never
   from the source checkout (which you may have advanced without installing).
   `update.sh` runs from a temporary copy of itself, so the checkout it
   advances cannot change the script mid-run.
2. **Builds the new release in its own directory** with its own venv, from
   `requirements.lock`, and **validates it against your config**. If that
   fails, nothing running has changed. A release directory that already
   exists — the current one, the previous one, anything a backup points at —
   is never written to again.
3. **Backs up** to `/var/backups/homelab-panel/<id>/`: the database via
   SQLite's online backup API (consistent even mid-write, integrity-checked),
   `config.yaml`, `.env`, the manifest, `panelctl` and the systemd unit. The
   backup records the list of files it holds, checksums over exactly that list,
   and a completion marker written last. Anything that fails verification is
   never a rollback candidate.
4. **Switches** — one atomic symlink flip and one restart — then **verifies**
   the service answers `/healthz` as `ready` with the expected version. If it
   does not, the service is stopped (and proven stopped), everything in the
   backup is put back — the database included, because a migrated database is
   one the previous release refuses to open — and the previous release
   restarts.
5. **Migrates the database forward** on first start. History, mutes and
   events survive; see `app/migrations.py`.

Every run of `install.sh` against an existing installation takes the same
backup, so a direct reinstall has the same undo as an update.

Your `config.yaml` is never overwritten. If the release added options you
don't have, it lists them at the end and leaves the decision to you.

| Command | What it does |
|---|---|
| `sudo panelctl update` | Update to the newest **stable** release |
| `sudo panelctl update --channel pre` | Include pre-releases (`v1.2.0-rc.1`) |
| `sudo panelctl update --channel main` | Track `main` instead of releases |
| `sudo panelctl update --to v1.2.0` | Update to a specific version |
| `sudo panelctl update --check` | Say what would happen, including whether `--allow-major` would be needed; change nothing |
| `sudo panelctl update --list-backups` | Show what can be rolled back to, and whether each backup verifies |
| `sudo panelctl update --rollback [id]` | Undo the last update, or restore a specific backup |
| `sudo panelctl status` | What is deployed, is the service up, does it answer |

After an update your clone sits at the release tag (detached HEAD). That is
fine for a deployment checkout; `git switch main` if you want to develop in it.

**Rollback** verifies the backup's checksums and database integrity, shows
you which backup and when it was taken, stops the service and confirms it has
stopped, then puts back the release directory recorded in the backup — still
on disk with its venv intact, so no network is needed — plus the database,
config, `.env`, `panelctl` and the unit, and verifies health before saying
anything succeeded. It snapshots the current state first, so a rollback is
itself reversible. **A rollback discards every reading, event and mute
recorded since the backup was taken** — that is what restoring a database
means. There are no down-migrations by design: restoring a backup always
works, and a reverse migration would have to guess what your monitoring
history meant.

The five most recent backups are kept under `/var/backups/homelab-panel/`.
They live on the same host as the panel; for host loss, copy that directory
somewhere else on a schedule, and try `--rollback` once before you need it.

**Versioning.** Semantic: PATCH is fixes, MINOR adds collectors and options
and is always safe to take blind, MAJOR means a manual step is needed.
`update.sh` refuses to cross a major boundary without `--allow-major`, so an
unattended update can never silently need your attention.

**Knowing an update exists.** Enable the `updates` collector and the board
tells you itself. It compares against the newest *stable* GitHub release and
never goes worse than a warning — being a version behind is not an outage.

```yaml
updates:
  enabled: true
  interval: 21600
  repo: "Seth-Smithey/homelab-panel"
```

**What changed.** `sudo panelctl config-diff` lists config keys the installed
example has that yours doesn't. Every one has a working default.

---

## Credentials

Secrets live in `/opt/homelab-panel/.env` (mode 600) and are referenced from
`config.yaml` as `${VAR}`. Nothing sensitive belongs in the YAML.

Scope every token to read-only. Suggested minimums:

- **Proxmox** — an API token for a user with the `PVEAuditor` role. Give the
  *token itself* an ACL too: with privilege separation on (the default), the
  token has no permissions until you grant them, and the panel reports every
  endpoint as forbidden. SMART disk health needs `Sys.Audit`, which
  `PVEAuditor` includes.
- **UniFi** — a local-only admin with read access, or a UniFi OS API key.
- **Wazuh** — a read-only API user, not the default admin.
- **Backblaze** — an application key restricted to the backup bucket,
  `listFiles` only. It never needs read or write.
- **Cloudflare** — Account → Cloudflare Tunnel → Read. Nothing else.
- **Splunk** — a role with `rest_properties_get`.
- **Authentik / Jellyfin** — a read-scoped API token.
- **Pi-hole** — v6: an app password (Settings → Web interface/API); v5: the
  API token from Settings → API.
- **Wazuh indexer** (optional) — a read-only indexer role, not `admin`.
- **NUT** — only if your `upsd.users` requires a login; otherwise leave blank.
- **GitHub** (for the `updates` collector) — optional; a token with no scopes
  only raises the rate limit.

No credentials at all: `services`, `fleet`, `dns`, `certificates`, `wan`,
`host_metrics`, `updates`, and the heartbeat sender.

If a credential leaks, the blast radius is "someone learned your disk is 46%
full." That is deliberate.

---

## Reaching it

**Locally.** `http://<vm-ip>:8080`. Add an A record on your internal DNS
server pointing `panel.example.com` at the VM so the name works inside the lab
without touching your public DNS — the usual split-horizon pattern.

**Externally.** Front it with Caddy and publish through a Cloudflare tunnel —
see `deploy/Caddyfile.example`. Two things matter:

1. `flush_interval -1` on the reverse proxy. The panel streams updates over
   SSE; without this the board only refreshes when a buffer fills.
2. Put a Cloudflare Access policy on the hostname.
   Access is the authentication story. The built-in `server.api_token` is a
   backstop, not a substitute.

Set `server.api_token` in config.yaml if the port is reachable by anything you
don't control — generate one with `sudo panelctl gen-token`.

Clients pass it as `Authorization: Bearer <token>` or `?token=<token>`. The
browser sends the query form exactly once, then trades it for an HttpOnly
session cookie and drops it from the URL, so the token stops being written
into the access log and browser history on every poll.

**On a phone.** The panel ships a web manifest, so "Add to Home Screen" gives
you a standalone app. It's built for a narrow viewport.

---

## Using it

- **Worst first.** Panels sort by severity, so problems rise to the top.
  Press `o` (or "Fixed order") to keep them in place during an incident.
- **Stale is not green.** A panel whose reading is older than three poll
  intervals shows its old values, labelled, but counts as *unknown* — a
  reading from ten minutes ago says nothing about now. If the board itself
  loses its connection, a banner says so and the verdict dims.
- **The strip.** One cell per check, the whole lab in one glance. Click a cell
  to jump to its row.
- **Click any row** for a sparkline and 24-hour availability.
- **Mute** a check you already know about — a box that's deliberately off, a
  probe that fails for a reason. Muted checks keep reporting and stay visible,
  they just stop driving the headline and stop paging you. Default is 24 hours.
  This is what stops a permanently-amber board from training you to ignore it.
- **Keyboard**: `/` filter, `p` problems only, `o` fixed order, `t` theme, `Esc` clear filter.

---

## Who watches the watcher

The panel cannot report its own death, and if it shares a host with something
it monitors, it goes dark exactly when you need it. Two answers:

1. **Run it somewhere boring.** A small dedicated VM on your main hypervisor
   is the honest default. That still leaves that hypervisor as a blind spot,
   which is what (2) is for.
2. **Enable the heartbeat.** Set `heartbeat.url` to a Healthchecks.io check, an
   Uptime Kuma push monitor, or an ntfy topic. The panel pings it every five
   minutes; if the pings stop, that service tells you.

The heartbeat fires unconditionally, even when panels are critical. It means
"the panel is alive", not "the lab is healthy" — conflating the two would let a
lab-wide outage silence the one signal proving your monitoring still works.

---

## Alerts

Off by default. Set `alerts.enabled` and a `webhook_url` and the panel posts on
transitions only — once when something breaks, once when it recovers. No repeat
spam, no alerts during the startup grace period, and never for a muted check.

The payload carries both `content` and `text` keys, so Discord and Slack-style
receivers both work without an adapter.

Delivery is retried — three times, at 30s, 2m and 10m — and a failure is
never treated as delivered: the check stays "unnotified" until a webhook
accepts it, so the next successful attempt still carries it. The footer shows
how many alerts are waiting or could not be delivered. The startup grace
period *delays* notification rather than cancelling it: whatever is still
wrong when grace ends gets one alert then.

**How a check's life works.** A check that appears already failing (a new
failed task, a newly disconnected agent) alerts on first sight. A check that
was present and disappears from a *successful* poll of its collector has
resolved — it gets a recovery alert if it was bad, and is forgotten, so the
same id failing again later is a fresh alert. A *failed* poll infers nothing:
a dead API did not fix your VM. A collector that cannot be reached is itself
a check (`panel.<name>`) and alerts like any other.

---

## Long-term history

The panel keeps a rolling window: metrics for 14 days, state transitions for
28. It is a live view, not an archive.

For real retention, scrape `GET /metrics` — standard Prometheus exposition,
including per-check severity as a gauge. Splunk can ingest the same endpoint.

---

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/status` | Full snapshot: panels, checks, headline, counts |
| `GET /api/stream` | Server-sent events, pushed on every collector run |
| `GET /api/history/{check_id}` | Metric history for one check |
| `GET /api/availability/{check_id}?hours=24` | Percent of window per severity |
| `GET /api/events` | Recent state transitions |
| `GET /api/mutes` · `POST`/`DELETE /api/mutes/{check_id}` | Manage mutes |
| `POST /api/refresh/{collector}` | Poll one collector immediately |
| `GET /metrics` | Prometheus exposition |
| `GET /healthz` | Liveness and version, no auth required |
| `GET /api/version` | Running version, config version, schema version |
| `POST /api/session` | Trade `?token=` for an HttpOnly session cookie |
| `GET /api/stats` | Database row counts and file size |
| `GET /api/config` | The running config with every non-publishable value redacted |
| `GET /api/sparklines?ids=a,b` | Recent values for several checks in one call |

---

## Adding a collector

1. Create `app/collectors/<name>.py` with a class that subclasses `Collector`,
   sets `key` (must equal the YAML section name) and `title`, and implements
   `async def collect(self) -> Panel`. Read options with `self.opt("...")`.
   Use `self.client()` for HTTP — it caches an `httpx.AsyncClient` per
   verify/timeout pair and honours `verify_ssl`. Raise `CollectorError` for an
   expected, explainable failure; raise anything else and `run()` turns it
   into a clean error panel rather than a traceback. Auth-token caching is
   your own business (see `pihole.py` or `wazuh.py` for the pattern).
2. Add the class to `REGISTRY` in `app/collectors/__init__.py` — order there
   is the order panels appear on the board.
3. Add its required keys to `REQUIRED` in `app/validate.py` (and to
   `SCHEME_REQUIRED` if `host` is an http(s) URL).
4. Add every *non-secret* option name to `PUBLISHABLE_KEYS` in
   `app/config.py`, or `/api/config` will show it as `***`. Secrets need no
   entry — anything not listed is redacted by default.
5. Add a documented block to `config.example.yaml` (leave it `enabled: false`
   if it needs credentials) and a row to the table at the top of this README.
6. Emit the same check ids whether things are healthy or not. A check that
   only exists when something is wrong still works — the engine treats its
   first sighting as a transition and its disappearance from a successful
   poll as recovery — but a stable id is easier to mute and graph.
7. Run `ruff check app/ tests/ && pytest -q`. Add a test if the collector has
   logic worth protecting (thresholds, parsing quirks).

Each collector runs in its own loop. A hung controller cannot stall the
others; the worst case is one panel going stale, which the UI labels
explicitly instead of quietly showing old numbers as current.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Panel is grey with "Cannot reach X" | Host down, or the credential is wrong. Check the journal for the collector name. |
| Panel shows "Last reading is Nm" | Collector is timing out. Its interval passed three times without a result. |
| Board only updates on refresh | SSE is buffered. Add `flush_interval -1` to the reverse proxy. |
| Service returns 401 but is fine | Expected behind Authentik or Access. Add `401` to that check's `expect_status`. |
| Everything critical on boot | Startup grace hasn't elapsed and collectors are staggered. Give it one interval. |
| Fleet says "no reply" but the host is up | In TCP mode a firewall that drops rather than refuses looks identical to a dead host. Use `fleet.method: icmp`, or set a per-host `port` (or the top-level `tcp_port`) to something that answers. |
| A failing collector polls less often over time | Working as intended. After three consecutive failures it backs off, capped at 10 minutes, so a box that's been down for an hour isn't probed every 20 seconds. |
| Fleet says every host is down | Under the hardened unit `ping` has no capabilities and relies on `net.ipv4.ping_group_range`. If that sysctl is narrowed on your host, set `fleet.method: tcp`. |
| The service won't start after an update | It should already have reverted itself; check `sudo panelctl status`. If not, `sudo panelctl update --rollback`, then open an issue with the journal output. |
| `--check` says a credential is missing but it's in `.env` | Check the file is at `/opt/homelab-panel/.env` and the key names match `deploy/env.example` exactly. |

```bash
sudo panelctl status        # what is deployed, is it up, does it answer
sudo panelctl logs
sudo panelctl check
sudo panelctl config-diff
```

---

## Developing

```bash
pip install -r requirements.lock -r requirements-dev.txt
ruff check app/ tests/
pytest -q
```

`tests/` holds a regression suite built from three review passes: every
scenario that was once a defect — a lost alert, a false green, a crash on a
bad config value — is a test. CI runs it on Python 3.10–3.12, audits the
lockfile with `pip-audit`, shellchecks the scripts, and runs the real
`install.sh` → `update.sh` → `--rollback` round trip in a container. A release
tag is refused unless all of that passes on the tagged commit.
x
