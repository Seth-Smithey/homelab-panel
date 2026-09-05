# Contributing

Thanks for looking. This is a small project with a specific job — one board
that says whether anything is wrong right now — so the bar for a change is
"does this make that answer faster or more trustworthy".

## Setup

```bash
git clone https://github.com/Seth-Smithey/homelab-panel.git
cd homelab-panel
python3.11 -m venv .venv && . .venv/bin/activate   # 3.11 or newer
pip install -r requirements.lock -r requirements-dev.txt
```

Run it locally against a scratch config:

```bash
cp config.starter.yaml config.yaml
PANEL_CONFIG=config.yaml PANEL_DB=/tmp/panel.db python -m app.main
```

## Before opening a pull request

```bash
ruff check app/ tests/
pytest -q
shellcheck --severity=warning install.sh update.sh deploy/panelctl deploy/panel-lib.sh tests/smoke/*.sh
```

CI runs the same, plus `pip-audit` and a real install → update → rollback
round trip. A PR that fails CI will not be merged; a PR that adds a test for
what it fixes is much more likely to be.

## Adding a collector

The checklist is in the README under "Adding a collector". The two things
people miss: `key` must equal the YAML section name, and non-secret option
names must go in `PUBLISHABLE_KEYS` or they show as `***` in `/api/config`.

## Changing the database

Add a migration to `app/migrations.py`, bump `SCHEMA_VERSION` in
`app/version.py`, and never edit a released migration. Migrations are
forward-only and run inside a transaction; there are no down-migrations —
rollback restores the pre-update backup instead.

## Changing a dependency

Edit `requirements.txt`, regenerate `requirements.lock` (instructions are at
the top of that file), run `pip-audit -r requirements.lock`, then the tests.

## Releasing

Bump `__version__` in `app/version.py`, add a section to `CHANGELOG.md`, tag
`vX.Y.Z`, push the tag. The release workflow runs CI against the tagged
commit and publishes only if it passes and the tag matches the code.
