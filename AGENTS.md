# Project workflow

- Develop and push changes to `dev`. The user uses that branch to track and validate work.
- Do not push or merge changes to `main` until the user explicitly confirms that `dev` is good and authorizes promotion.
- Preserve monitoring semantics during UI changes: stale, unknown, muted, pending, and unreachable collectors must remain distinguishable from healthy checks.
- Validate frontend changes with the populated dashboard test and the existing browser smoke test, in addition to JavaScript syntax checks.
