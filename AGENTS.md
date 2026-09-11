<!-- bmad:context -->
<!-- Verified 2026-09-11 against f340fa8. Managed by bmad-project-context; edits inside this
     block are replaced on refresh. Keep anything you want preserved outside the markers. -->

## vahub

Self-hosted voice assistant hub: an agent loop, a policy gate, a module supervisor, a
scheduler, a SQLite store and a web console. Python 3.12+, uv, FastAPI, pydantic, aiosqlite.
The gate is the point of the project; everything else exists so the gate has something to
authorize.

## Policy

- Never reach a module except through `ModuleAPI.call` in `src/vahub/core/moduleapi.py`. It is
  the only caller of `Gate.evaluate`, so the agent loop, the scheduler and a confirmed
  destructive replay all pass the same check; a new path that calls a module directly
  bypasses the gate.
- Bump the schema version and add a `CHANGELOG.md` migration note when changing
  `src/vahub/config/` or `src/vahub/contracts/`. Other people's manifests, registry entries
  and config files depend on them.

## Where things are

- Documentation goes in the separate `vahub-docs` repo, not here. This repo keeps only
  `README.md`, `CONTRIBUTING.md`, `SECURITY.md` and `CHANGELOG.md`.
- A new capability (lights, calendars, transit, notifications) is a module in the separate
  `vahub-modules` repo, not a change to the hub. The hub stays small on purpose.
- Commit format, pull request expectations and what a change should look like:
  `CONTRIBUTING.md`.
- BMAD artifacts: `_bmad-output/`, gitignored and local.

## Running and verifying

- Run `uv run pre-commit run --hook-stage pre-push --all-files` before pushing. A plain
  `uv run pytest` omits CI's `--strict-markers`, so a test carrying an undeclared marker
  passes here and fails in CI.

## Conventions that differ from defaults

- Module output is untrusted text: guard it before use, never render it as markup, and build
  subprocesses from argv lists.

<!-- /bmad:context -->
