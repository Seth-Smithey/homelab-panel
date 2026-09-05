# Security

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's
private vulnerability reporting on this repository ("Security" tab → "Report
a vulnerability"). Include the version (`sudo panelctl version`), what you
found, and how to reproduce it. You should hear back within a week.

## What the panel is, and is not, designed to resist

The panel is a read-only monitoring board for a home lab. Its threat model:

- **Every credential it holds is read-only by design.** The documented
  minimum scopes (README → Credentials) mean a leaked `.env` tells someone
  your disk is 46% full, not lets them stop a VM.
- **It runs as an unprivileged service account** under a hardened systemd
  unit: no capabilities, `ProtectSystem=strict`, `ProtectProc=invisible`,
  native syscall ABI only. It can write to its own `data/` and nowhere else.
- **The API defaults to unauthenticated** because the intended deployment is
  a trusted LAN or behind Cloudflare Access. Set `server.api_token` if the
  port is reachable by anything you do not control. The token is compared in
  constant time, exchanged once for an HttpOnly cookie carrying a random
  session id (the token itself is never stored in the browser; the server
  stores only the id's hash), and never written to the access log by the
  shipped client. Rotating the token invalidates every session.
- **State-changing requests** (mute, refresh) require a same-origin request
  or a bearer header, so a hostile page cannot drive them.
- **`/api/config` is redacted by allow-list**: any key not explicitly marked
  publishable is hidden, so a newly added secret is safe by default.

Out of scope: a compromised panel host, a compromised reverse proxy, or an
attacker who already has read access to `/opt/homelab-panel/.env`.

## Dependencies

`requirements.lock` is the exact set installed. CI runs `pip-audit` against
it on every push and refuses a release with a known vulnerability. Dependabot
proposes bumps weekly.
