# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and this project uses
[semantic versioning](https://semver.org/): PATCH is fixes, MINOR adds
collectors and options and is always safe to take blind, MAJOR means an
update needs a manual step.

## [1.0.0-rc.1] — 2026-09-05

First tagged release, as a release candidate: the code has had three review
passes and a regression suite, but has not yet run against live Proxmox,
UniFi, Wazuh, Splunk, UPS, Pi-hole, Cloudflare or Backblaze services. `1.0.0`
is the tag to cut after it has survived a week on real hardware.

### Security — CodeQL findings on the first push

- **Session cookie no longer carries the API token.** It is now a random
  session id; the server stores only its SHA-256 plus a fingerprint of the
  token it was issued under, in a persisted `sessions` table (schema v5), so
  a restart keeps browsers logged in and rotating `server.api_token` logs
  every browser out. A guessed cookie, or the token used as a cookie, is 401.
- `certificates` pins TLS 1.2 as the floor for its inspection connection (the
  deliberately unverified handshake is documented in place: it reads the
  certificate, it trusts nothing and sends nothing).
- CI workflow declares `permissions: contents: read` at the top level.
- Tests no longer use `tempfile.mktemp` (predictable-name race); each test
  file lives in its own `mkdtemp` directory.

### Fixed — pre-push scan against the live repository

- `install.sh` validated the staged release by executing its `deploy/panelctl`
  directly; a commit made from Windows has no executable bit, so an update to
  such a commit failed with "Permission denied". Everything now runs repo
  scripts through `bash`, and the smoke test's "newer release" is committed
  with every executable bit stripped to keep it that way.
- `install.sh` preferred `python3.12` over `python3` on Ubuntu 24.04 and then
  could not self-heal a missing `python3-venv` (the apt step only knew the
  bare interpreter). It now prefers the distro `python3` when it is new
  enough, installs the matching `python3.X-venv` package when a venv cannot
  be built, and copes with a host that has no python at all.
- `update.sh` ran git as root under `umask 077`, leaving the user's clone
  root-owned and unreadable to them. Git now runs as the clone's owner with a
  normal umask; mode-only differences no longer count as a dirty checkout.
- `--channel pre` sorted `v1.0.0-rc.2` above `v1.0.0` and would have offered
  a downgrade the day the stable release shipped (`versionsort.suffix=-`).
  Moving to an older build now requires `--allow-downgrade` (rollback is the
  right tool for that); `--check` says so. "No release tagged yet" exits 0.
- A first install that never became healthy left the unit enabled to
  crash-loop on the next boot; `revert()` now disables it.
- `services`: a scalar `expect_status: 401` passed validation and then raised
  inside the probe, replacing the check with a warning whose id changed every
  poll. Scalars are accepted; a failed probe is `svc.<name>` / unknown.
- `certificates`: `hostname:abc` errored the whole panel instead of one check.
- Alerts: with `min_severity: critical`, a sent downgrade (critical → warning)
  now also gets its recovery sent; the receiver's last word is never stale.
- `updates` collector: with only pre-releases published, GitHub's
  `/releases/latest` answers 404 — it now falls back to the release list and
  labels a pre-release. Its check id moved from `panel.version` (the reserved
  reachability namespace) to `updates.version`.
- Release notes extraction used a regex through `awk -v`, which gawk and mawk
  process differently; it is now a literal prefix match. The release's fresh
  install snippet clones the tag being released.
- Post-update "new config options" report only appears when the release
  actually changed `config.example.yaml`.
- `/healthz` answers HEAD; a few more non-secret keys are publishable in
  `/api/config`; `wan.prefer_ipv4` documented; README corrections.

### Changed — supported Python

- **Python 3.11 or newer is required** (Ubuntu 24.04 ships 3.12). `websockets`
  17, pulled in by `uvicorn[standard]`, dropped 3.10, and 3.10 reaches end of
  life in October 2026 — holding the pin back would recur with every
  dependency that follows. `install.sh` now refuses an older interpreter with
  instructions instead of failing inside pip, and prefers the newest
  `python3.x` on the host so a deadsnakes install on 22.04 works.
- Dev tooling: `pytest` 9.0.2 → 9.0.3 (PYSEC-2026-1845, the Dependabot alert
  on the repository; the runtime lockfile was never affected).

### Changed — correctness under failure (round 3)

A third written review tested the failure paths rather than the happy path:
a failed schema-changing update, a webhook outage spanning several
transitions, partial integration responses. Each of these was wrong, and
each is now a regression test.

- **A failed update could take both releases offline.** The new release
  migrated the database on startup; when it then failed readiness, the old
  release was restored but refused to open the newer schema. Activation is
  now one transaction: a verified backup of the database, config, `.env`,
  manifest, `panelctl` and the unit is taken immediately before the switch,
  and on failure the service is stopped *and proven stopped*, everything in
  the backup is restored, and the previous release restarts. `install.sh`
  and `update.sh` share this logic (`deploy/panel-lib.sh`) and one lock.
- **Backups could be marked COMPLETE after a failed copy** (command
  substitution drops `set -e`), excluded `.env` from the checksums, and were
  never verified on restore. A backup now records the list of files it holds,
  checksums exactly that list, re-verifies the checksums before writing the
  marker atomically, and `--rollback` refuses anything that fails
  verification before it changes a byte. `--list-backups` shows which verify.
- **Rollback restored the database even when the service had not stopped.**
  It now requires a confirmed stop and aborts, changing nothing, otherwise.
- **Reinstalling the current commit rsynced and ran pip inside the running
  release.** An existing release directory is never written to again; the
  current one is re-validated and re-activated untouched (or, if healthy and
  unchanged, left alone), a retained rollback target is rebuilt under a new
  id, and only an unreferenced leftover is replaced.
- **`update.sh` checked out the new commit over the file Bash was still
  reading.** It now runs from a temporary copy of itself.
- **`update.sh --check` failed on a major-version bump** instead of reporting
  that `--allow-major` would be needed. A plan is never an error.
- **Partial collector results manufactured recovery notifications.** When a
  detail endpoint (Wazuh agents, Proxmox disks or tasks, Splunk sections)
  failed while the summary succeeded, the checks it would have produced
  vanished and were announced as resolved. Collectors now declare unread
  sections (`Panel.unread`) with a visible placeholder; the engine infers
  nothing about checks under an unread prefix.
- **A stale retry could deliver an old outage after its recovery** ("ok, then
  critical"). Notification is now a *debt*: every check whose observed
  severity differs from its notified severity is owed one notification
  describing the transition from what was last delivered to what is true
  now, settled in one batch built from current state. Retries never replay an
  old message; debts survive restarts because both sides are persisted.
- **Disappearance during startup grace lost the recovery for good.** A
  vanished check is now a tombstone that owes its recovery until delivered.
- **Restored state for disabled collectors was reconciled.** It is dropped at
  startup with a log line; nothing will ever refresh it.
- **Muted or below-floor batches counted as delivered; 3xx counted as
  delivered.** Mutes defer the debt (a fault that outlives its mute is
  reported when the mute expires; a muted recovery is settled silently), the
  severity floor settles silently, and only HTTP 2xx is delivery.
- **An empty Wazuh manager response read as "all running".** No daemon data
  is now `unknown` with the likely cause. Agent and Cloudflare tunnel
  inventories are paginated to the end; every failed Proxmox task in the
  window is its own check with a total on `pve.tasks`.
- **Event-like checks (failed tasks, noisy rules) "recovered" when they aged
  out of their window.** They are now `ephemeral` (schema v4) and are
  forgotten silently.
- **Validation accepted `"8080.0"` and `"3e1"`** that the runtime's `int()`
  then rejected; `proxmox: true` bypassed validation; `enabled: "false"`
  enabled a collector; `"prefix-${UNSET}"` silently became `"prefix-"`.
  Integers must be integer literals, collector sections must be mappings
  with real booleans, and any unresolved `${VAR}` anywhere in a value marks
  the whole value missing.
- **Availability claimed 100% coverage across monitoring gaps.** Each
  successful poll is now logged (`observations`, schema v4); time between
  polls more than three intervals apart is unobserved, and the response says
  whether coverage is measured or assumed.
- **The client declared connection loss after 45s of healthy quiet.** The
  stream now sends named `heartbeat` events; the watchdog tracks transport,
  not reading freshness. A reopened chart could show an older poll; the
  chart cache is now keyed by the poll it was loaded for. Snapshots carry the
  build version and an open board offers a reload after a server update.
- `/metrics`: stale checks export `unknown` plus `homelab_check_stale`;
  `homelab_panel_since_success_seconds` and `homelab_panel_stale` added;
  `homelab_panel_age_seconds` is documented as time since the last attempt.
- Row disclosure is a real button with `aria-controls`; the expanded region
  shows the full diagnostic text; the icon column is the icon's width;
  same-day formatting uses the configured timezone; `[::1]` for IPv6 health
  checks; the venv preflight builds a real venv; `iputils-ping` is checked;
  the release workflow refuses an empty changelog section; CI checks
  executable bits and LF endings, and runs the full install → failed update
  (reverted) → update → refused damaged rollback → rollback rehearsal
  (`tests/smoke/install-update-rollback.sh`, runnable on a real VM).

### Changed — the install and update model (round 2)

The updater was not safe to rely on and has been rebuilt around an immutable
release layout. Nothing running is touched until a new release has been built
in its own directory and validated against your config.

- **The installer deleted the updater's backups.** They lived under
  `/opt/homelab-panel/backups/`, inside the directory `rsync --delete`
  synchronised. Every update destroyed its own recovery point, including the
  one it had just taken. Backups now live in `/var/backups/homelab-panel/`,
  outside anything the installer touches, with an id, a manifest, checksums
  and a completion marker; an incomplete backup is never a rollback candidate.
- **Updates modified the running install before validation.** Source files,
  the shared venv and config were all replaced in place, so a failed
  dependency install or validation left the live service running against
  changed code. Each release now builds in `releases/<commit>/` with its own
  venv; `current` is a symlink flipped atomically after validation; a failed
  health check flips it back.
- **Rollback trusted the source checkout's HEAD**, not what was deployed, so
  advancing the clone without installing made rollback restore the wrong
  code. `manifest.json` now records the deployed version, commit, release
  directory and schema at activation; rollback restores exactly that, without
  network, and verifies health before reporting success. A pre-rollback
  snapshot makes rollback itself reversible.
- **`ProcSubset=pid` broke the one default collector.** It hides
  `/proc/stat`, `/proc/meminfo` and `/proc/uptime` — what psutil reads — so a
  fresh install's host-metrics panel failed inside the unit while passing by
  hand. Removed; `ProtectProc=invisible` stays.
- **Validation could import the wrong code.** `python -m app.main` resolves
  `app` against the current directory. New `panelctl` wrapper runs the
  installed release, from its directory, as the service user, with the
  service's environment; every documented command uses it.
- **Prereleases and unpublished tags were update candidates.** The stable
  channel now accepts only `vX.Y.Z`; `--channel pre` includes suffixes.
  `parse_version` orders `1.0.0-rc.1` before `1.0.0` instead of equal to it.
- **The version path assumed the repo root.** It is now derived from
  `git rev-parse --show-prefix`, so a subdirectory checkout works. Missing or
  malformed target metadata aborts before checkout instead of guessing.
- **The port preflight exempted every process named `uvicorn`.** It now
  exempts only this service's PID. `curl` is installed as a dependency; the
  health check reads the bind address from the panel's own config parser and
  requires `ready` plus the expected version, not merely a 200.
- One `flock` for the whole update/rollback transaction. `--check` no longer
  creates directories. `--list-backups` and `--rollback <id>`.
- **Dependencies bumped and audited.** `pip-audit` on the previous pins found
  published vulnerabilities in `starlette 0.41.3` and `cryptography 44.0.0`.
  A `requirements.lock` now freezes the full resolved set and CI audits it.
- **Releases are gated on CI.** The release workflow runs the full test suite
  against the tagged commit and refuses to publish on failure.

### Fixed — alert lifecycle (round 2)

- **Startup grace lost failures permanently.** A check that went critical
  during grace was recorded but never notified, and never would be — the
  state did not change again. Observed and notified severity are now tracked
  separately; when grace ends, every check still out of step is notified
  once. `startup_grace: 0` now means zero (`or 120` made it mean 120).
- **Disappearing checks had no lifecycle.** A disconnected agent's row
  vanished on recovery with no recovery alert, the cache kept it critical, and
  the same agent disconnecting again was "unchanged" — no repeat alert. A
  check absent from a *successful* poll now resolves (recovery alert if it was
  bad) and is forgotten; a *failed* poll infers nothing.
- **Collector reachability was not actually persisted**; only `panel.checks`
  reached the database. `panel.<key>` states are saved too.
- **Alert delivery was never retried.** A webhook timeout lost the
  notification forever. Three retries with backoff; a failure is never marked
  as delivered; delivery health is in the snapshot and the footer.
- **One non-finite metric broke `/api/status` and the stream.** Checks are
  sanitised at the model boundary: a NaN/inf `metric` or `percent` becomes
  absent and the check unknown; `extra` is scrubbed too.
- **Stale results kept a green verdict.** A stale panel's OK now degrades to
  unknown in the snapshot and in `/metrics`; the client has a watchdog and a
  banner for a dropped connection.
- **Availability counted time never observed.** `first_seen` bounds the
  window; the response reports coverage separately from health.
- **The validator crashed on ordinary mistakes** (`max_age_hours: oops`).
  Every value is type- and range-checked before conversion; conditional
  credentials (UniFi login mode, Pi-hole v5/v6) are validated; threshold pairs
  are checked for warn > crit.

### Fixed — auth and integrations (round 2)

- **A stale session cookie blocked a rotated token.** Explicit credentials
  (header, then query) now outrank the cookie; a rejected stale cookie is
  cleared on the 401.
- **The token stayed in localStorage** after the HttpOnly exchange. It is
  exchanged via the Authorization header (so that request is not logged
  either), then removed from storage and the address bar.
- **The origin guard accepted `same-site`** (sibling origins) and skipped
  requests without Fetch Metadata. It now accepts only `same-origin`/`none`,
  validates `Origin` against `Host` when Fetch Metadata is absent, and treats
  a bearer header as proof of a deliberate client.
- **Pi-hole ignored `verify_ssl`** — every request hard-coded `verify=False`.
- **Authentik `/admin/workers/` was deleted in 2025.8.** The collector tries
  `/tasks/workers` first and falls back; outposts use the dedicated health
  endpoint and treat `last_seen` as connected only when recent.
- **Any uploaded object counted as a backup.** Targets accept `pattern` and
  `min_bytes` (defaults match vzdump archives ≥ 1 MB); an incomplete listing
  reports unknown rather than a definitive age.

### Fixed — client (round 2)

- **"Problems only" hid failed collectors.** Panels with an error, stale
  reading, empty result or pending first poll survive the filter.
- **Keyboard focus was lost on every frame**; it is now restored by identity.
- **Every open chart refetched on every frame**; charts reload only when their
  own panel produced a new reading, and are cached otherwise.
- **Mute and refresh failed silently.** Actions report errors in a toast;
  401 shows a session-expired state. `refreshNow` goes through the same
  ordering guard as the stream. Bootstrap requests are bounded.
- **Graphs plotted equally spaced** regardless of time. They now use real
  timestamps, break the line across gaps, and label unit and time range.
- **Contrast** for muted text and light-theme warning text raised above
  4.5:1; muted rows are de-emphasised by border, not whole-row opacity.
- Severity glyphs and labels alongside colour; a strip legend; larger touch
  targets; long values wrap on phones; fixed-order toggle (`o`); the site's
  configured timezone; dates in the change log; reduced-motion respected;
  live-region announcements; the Notify button says plainly that it is
  browser-only.

## Pre-release history — first review pass

The section below was written for the first tagged release before the second
review pass. Everything in it stands; the version was renamed to a release
candidate because none of it had yet run against live services.

### Added — updating

The panel can now be updated in place rather than reinstalled.

- **`update.sh`.** Backs up the database (SQLite's backup API, consistent
  even mid-write), `config.yaml` and `.env`; checks out the newest release
  tag; migrates; verifies the service comes back healthy; and rolls itself
  back automatically if it does not. `--check`, `--to`, `--channel main`,
  `--rollback`, `--allow-major`.
- **Database migrations** (`app/migrations.py`), forward-only and tracked in
  SQLite's `PRAGMA user_version`. History, mutes and events survive an
  update. Refuses to run an old build against a newer schema, which is how
  data gets silently corrupted after a downgrade.
- **Semantic versioning** with one source of truth (`app/version.py`). The
  release workflow refuses a tag that disagrees with it, so `update.sh`, the
  in-app check and the GitHub release can never describe different code.
- **`updates` collector** — asks GitHub for the newest release and shows it
  on the board. Off by default, never worse than a warning, fails quiet on a
  rate limit.
- **`--config-diff`** lists config keys the current example has that yours
  does not, so a release cannot add a feature you never hear about.
- **`config_version`**, checked by the validator.
- **Release workflow** publishing GitHub Releases from the changelog on tag.
- **CI** now also tests the migration path, config validation for both
  shipped configs, a secret-redaction canary, and that auth actually rejects.

### Fixed — a fresh install could not start

The installer copied `config.example.yaml`, which enables Proxmox, Wazuh,
Backblaze and the rest. Their credentials expanded to empty strings, startup
validation exited, and the installer left an enabled service crash-looping —
before the README had told you to enter any credentials.

Installs now begin from `config.starter.yaml`: host metrics only, no
credentials, always comes up green. The installer validates before starting
anything and stops cleanly if the config is wrong rather than enabling a
failing unit.

### Fixed — alerts that never fired

- **A newly appearing failure never alerted.** Transitions were only recorded
  for checks already in the severity cache. Several collectors only create a
  check when something is wrong — Proxmox one per failed task, Wazuh one per
  non-active agent — so those failures appeared already-bad, were treated as
  "not a transition", and never notified. Ever: they keep the same severity
  afterwards, so no later change fires either.
- **Check state did not survive a restart.** The cache was in-memory only, so
  after any restart every check looked new. A failure that began during the
  restart produced no event and no alert, and `availability` then reported
  100% healthy for a check that had been down for hours. Now persisted.
- **A collector going dark never alerted.** An expired token or unreachable
  controller returns an error panel with no checks, so every check simply
  vanished and nothing transitioned. Collector reachability is now a
  first-class alertable transition.
- **Panel event states were reversed** — entering an error recorded
  `error → error` and recovery `reachable → reachable`.
- **Alert failures were invisible.** The task's exception was never
  retrieved, so alerting could stop working silently.
- **In-flight alerts were destroyed at shutdown**, losing the notification
  for the failure that may have caused the shutdown.

### Fixed — the board could say everything was fine when it wasn't

- A collector that answered but returned no checks rolled up to **green**.
  It is now unknown: an empty API response is uninformative, not healthy.
- `_headline()` fell through to **"Everything is healthy"** when the overall
  state was unknown — including before the first poll had completed.
- Pending panels were counted as unknown *checks*, so the header read
  "12 checks · 12 unknown" before a single check existed.
- **Proxmox failed-task queries swallowed every error**, so a permissions
  failure or timeout was reported as "nothing failed in the last 24h" — a
  clean audit that never ran. Partial coverage is now stated explicitly.
- **Same-named Proxmox storage on different nodes was discarded.** Every node
  has its own `local` and `local-lvm`, so only the first was ever checked and
  a full disk on another node was invisible.

### Fixed — security

- **A one-request permanent denial of service.** `POST /api/mutes/x?hours=inf`
  passed the `<= 0` guard, was committed to SQLite, and then made every
  subsequent response raise "Out of range float values are not JSON
  compliant" — a 500 on the entire API, surviving restarts, recoverable only
  by editing the database. Unauthenticated in the default config.
- **`/api/config` leaked `heartbeat.url`** — a push URL whose path is the
  credential, letting anyone keep your dead-man's-switch green while the
  panel is dead. Redaction is now an allow-list, so a newly added secret is
  hidden by default rather than exposed until someone remembers.
- **The API token was written to the access log on every request.** The
  client put it in every URL; uvicorn, Caddy and Cloudflare all log query
  strings. It is now exchanged once for an HttpOnly cookie.
- **An unset `${VAR}` became an empty string**, so `api_token: "${PANEL_TOKEN}"`
  with the variable unset silently disabled authentication on the whole API.
  Unresolved references are now distinguishable, and that specific case is a
  startup error.
- **Token comparison was not constant-time.**
- **`require_token` failed open** if config wasn't loaded.
- **No CSRF protection** on state-changing POSTs — a hostile page could mute
  a critical check.
- **Negative `limit`/`points` bypassed the clamps**; SQLite reads `LIMIT -1`
  as unlimited, so one request could dump the whole metrics table.
- The UPS collector shipped its **entire NUT variable dump** to every browser.
- systemd hardening: added `SystemCallArchitectures=native` (without it the
  seccomp filter is bypassable via a secondary ABI), `ProtectProc=invisible`,
  `ProcSubset=pid`, `PrivateDevices`, `UMask=0077`, `CapabilityBoundingSet=`
  and others. Added a CSP to the Caddy example. Application code is now
  root-owned, with the service account owning only its state.

### Fixed — performance and correctness

- **`prune` full-scanned the metrics table** (millions of rows) on the event
  loop, blocking every collector and request — the only index led with
  `check_id`, not `ts`. Same for `availability`. Both are indexed now.
- **Nothing pruned for the first hour**, and a service restarting more often
  than that never pruned at all.
- **`active_mutes()` wrote and committed on every read** — and it runs on
  every collector poll and every SSE broadcast, dozens of times a minute.
- **`/api/sparklines` was an N+1** of separate locked queries; now one.
- **`/api/refresh` raced the collector's own loop**, invalidating shared
  sessions and cookies. Now serialised per collector.
- **SSE could replay stale frames over fresh ones**, visibly reverting the
  board to a state that had already cleared.
- `--check` **did not load `.env`**, so the command the README and installer
  both print reported every credential missing against a correct file.
- `PANEL_PORT=8090 ./install.sh` **checked 8090 but the app still read 8080**
  from `config.yaml` and failed to bind. The port is now written to the
  config. The conflict check no longer exempts every process named `uvicorn`.
- Unbounded SSE subscribers; unretrieved alert-task exceptions; a store that
  could be used after close.

### Changed

- Renamed from "smitheylab panel" to "homelab panel"; example configs use
  neutral addresses and hostnames.
- Ruff configuration pinned in `pyproject.toml` so CI and local runs cannot
  disagree about what passes.

## Pre-release — initial correctness pass

A review pass over the whole tree before publishing. The theme running through
most of these: a monitoring tool that reports the wrong colour is worse than one
that reports nothing, because it trains you to stop looking. Several of these
bugs made the board green during a real failure, or permanently red during
normal operation.

### Fixed — the panel would not start

- **`PANEL_LOG_LEVEL=info` crashed the app at import.** `logging.basicConfig`
  rejects lowercase level names with a `ValueError`, and `deploy/env.example`
  — the file the installer copies to `.env` — shipped `info` in lowercase. The
  level is now normalised. (`app/main.py`)

### Fixed — the installer

- **Re-running `install.sh` always aborted.** The port preflight matched the
  panel's *own* running service and exited 1, contradicting the "safe to
  re-run" promise in its header.
- **The configured port was never actually read.** The awk expression used
  `\s`, a GNU extension that Ubuntu's default `mawk` does not support, so the
  port silently fell back to 8080 — and the same line unconditionally
  overwrote the `PANEL_PORT` environment variable the error message told you
  to set, making `sudo PANEL_PORT=8090 ./install.sh` a no-op.
- **`rsync --delete` wiped the venv on every re-run,** forcing a full rebuild
  and pulling the interpreter out from under the running service (203/EXEC).
- **A crash-looping unit still printed "Installed." and exited 0.** The
  installer now verifies the service stayed up and prints the journal if not.
- **The closing banner printed a literal `$(hostname -I | awk '{print $1}')`**
  instead of the URL — the one thing the installer exists to hand you.

### Fixed — checks that reported the wrong colour

- **Wazuh: a stock manager was permanently critical.** Every non-`running`
  daemon counted as stopped, including the six that ship disabled by default.
  Optional daemons are now excluded from the rollup.
- **Wazuh: an unreachable indexer discarded the agent and daemon checks.** The
  optional alerts section was the one block not wrapped, so an indexer restart
  replaced a fully-populated panel with a single error.
- **Wazuh: `never_connected` was critical while `disconnected` was only a
  warning** — backwards. A box that was reporting and went dark is the
  actionable case.
- **Wazuh: check IDs were built from `hash()`,** which is salted per process.
  Every restart silently orphaned mutes and metric history. Now SHA-1 based.
- **Proxmox: a storage that went unavailable vanished from the panel** instead
  of alerting. An absent check never transitions, so no event fired and no
  alert sent — the board stayed green with nowhere for backups to land.
- **Pi-hole: an expired v6 session read as "blocking disabled, 0 queries".**
  The responses were parsed without checking status, so a 401 body became a
  full-red DNS alarm on a healthy Pi-hole. Now re-authenticates once.
- **Backups: only the first page of B2 results was searched,** and B2 returns
  names lexicographically, not by time. Past ~1000 objects under a prefix the
  newest restore points sorted out of view and the target reported an
  ever-growing age while backups ran fine. Now paginated.
- **Authentik: a dead worker stack made the Identity panel green.** The
  `except Exception: pass` removed the check entirely, and a panel with no
  failing checks rolls up to OK — hiding exactly what the check was for.
- **UniFi: `upgrading` and `provisioning` counted as offline,** firing an alert
  storm across every device after any settings change, then a second on
  recovery.
- **UPS: a UPS reporting no status flags rendered "unknown" in green.**
- **Splunk: license usage was summed over all pools but divided by the quota of
  only some,** inflating the percentage and paging for phantom breaches.
- **WAN: DDNS never matched on a host with IPv6 egress.** The echo services
  returned a v6 address and the comparison queried A records only. Now prefers
  IPv4 and matches the record type to the address family.
- **Jellyfin: `transcode_warn` was dead config** — one transcode turned the
  panel yellow regardless of the threshold.

### Fixed — crashes and hangs

- **A hung NFS/CIFS mount froze the entire dashboard.** `psutil.disk_usage` is
  a blocking `statvfs` called inline on the shared event loop, so one bad mount
  stalled every collector and all HTTP handlers. Now threaded with a timeout.
- **One misconfigured DNS resolver wiped out the whole DNS panel.** dnspython
  rejects a hostname where an IP is required, and the setter sat outside the
  `try` with `gather` lacking `return_exceptions`.
- **`exc.request` cannot be truth-tested** — it raises when unset, so the
  intended fallback in the error handler would itself have thrown.
- **`IndexError` instead of a readable message** on empty-but-present API
  arrays (Wazuh RBAC, Splunk permissions, Cloudflare token scope).
- **The HTTP client cache ignored the timeout argument,** silently reusing a
  client built with a different one.
- **Alert tasks could be garbage-collected mid-flight** — the loop holds only a
  weak reference to a bare `create_task`.

### Fixed — the web client

- **The board never recovered live updates after a hard SSE close.**
  `EventSource` only auto-retries transport drops; a 502 during a service
  restart or a 401 closes it permanently. It fell back to 15-second polling
  forever, with only a manual reload as the fix — worst on a wall display.
  Now reconnects with backoff, plus an immediate retry on tab focus.
- **A notification threw on Android Chrome and froze the board** mid-render,
  precisely when something had gone critical.
- **Mute and Open were unreachable by keyboard** — the row handler's
  `preventDefault` cancelled their activation.
- **Expanded rows could stick on "Loading history…" forever** while issuing two
  requests per row per frame.
- **Global hotkeys fired with modifiers held**, so Ctrl+P both printed and
  toggled the filter.
- **A `javascript:` URL in `config.yaml` became a live executing link.**
- **An installed PWA 401'd on every request** because `start_url` drops the
  token from the query string. It is now remembered.
- **`.row-detail` was an inline span,** so its `overflow`/`text-overflow` rules
  did nothing and long details were hard-clipped with no ellipsis.

### Changed

- Panels awaiting their first poll no longer count as unknown *checks*, which
  made the header read "12 checks · 12 unknown" before any check existed.
- `POST /api/refresh/{collector}` now pushes to every open board, not just the
  caller's.
- A service worker is served from the root so the PWA is actually installable.
  It caches nothing on purpose — a stale status board is worse than none.
- Caddy no longer compresses `/api/stream`.
- Renamed from "smitheylab panel" to "homelab panel"; example configs use
  neutral addresses and hostnames.
