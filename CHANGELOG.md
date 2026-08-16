# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Until 1.0.0 the configuration file and the module contract may change between
minor versions. Anything that breaks an existing `vahub.yaml`, manifest or
registry entry is listed under Changed with what an operator has to do.

## [Unreleased]

### Added

* **Built-in accounts and login.** The hub can require its own sign in instead of
  relying only on a reverse proxy. Named accounts (scrypt-hashed passwords,
  revocable DB sessions in an HttpOnly SameSite=Strict cookie) are managed with
  `vahub user add/list/passwd/disable/enable/remove`; the hub never sets a
  password itself. `web.auth.enabled` defaults on, so a browser-reachable hub is
  not open. The audit log records which account confirmed an action.
* **Saved data the owner and the assistant can edit.** Locations (home, work),
  key/value preferences, and a memory the assistant can write to, all in the
  database. A built-in `core` module offers gated tools (set_location, remember,
  create_schedule, ...) so the agent can manage them through the same policy gate
  as any module; a signed-in owner edits the same data through origin-checked
  REST routes and the web UI. Policy rules and accounts stay file/CLI-only.
* **Runtime-editable schedules.** Cron routines can be created and removed at
  runtime (by the UI or the assistant) and are persisted. They still run as
  principal `scheduler`, so they are bounded by the scheduler's policy at run
  time regardless of who created them. File schedules stay read-only.
* **A real web UI.** Tabs for the assistant, Locations, Settings and Schedules,
  behind the login. Still rendered without `innerHTML`; the CSP nonce is bound to
  the inline tags.
* **New modules** (in vahub-modules): `weather` (Open-Meteo, no key) and
  `calculator` (safe arithmetic, no eval, no network).

### Changed

* Store schema v2 (accounts, sessions, preferences, locations, runtime
  schedules). Existing databases migrate on start.
* The reverse proxy no longer 404s `/api/schedules`, now a signed-in route.

## [0.1.0] - 2026-08-12

First release.

### Added

* A hub that runs an agent loop against an OpenAI compatible or Anthropic
  endpoint, with a mock provider for development and tests.
* A policy gate in front of every tool call. Default deny, per principal rules,
  constraints on argument values (`in`, `matches`, `range`, `max_len`), and tool
  classes of `read`, `write` and `destructive`.
* Out of band confirmation for classes an operator marks as needing it. The
  pending call keeps the arguments it was created with, so a later turn in the
  conversation cannot change what is executed, and it expires after
  `policy.confirm_ttl_s`.
* A module supervisor that spawns each module as a separate process, performs
  the MCP handshake over stdin and stdout, probes health, and restarts with
  exponential backoff and a failure count that is forgiven after a module has
  run cleanly for long enough.
* A module contract: a manifest declaring the command as an argv list, the
  configuration keys the process receives, health, restart and audit settings,
  and the tools the module claims to offer.
* A module registry client. `vahub module add` resolves a name through a JSON
  index or installs directly from a git, path or PyPI source. Git sources must
  be pinned to a tag or a commit sha.
* A scheduler for cron routines built from multiple steps, running through the
  gate as its own principal.
* A SQLite store holding conversations, pending confirmations and an audit
  record of every tool call, including the ones that were refused.
* A web interface that is only the assistant: a chat view, speech in and out,
  and the prompt to confirm a destructive action. Module state, stderr, the tool
  catalogue and the audit log are not served over HTTP; they are read with the
  CLI on the host. Alongside it, a small JSON API and a Prometheus endpoint.
* Speech input and output, either in the browser or through an OpenAI compatible
  transcription and synthesis endpoint.
* A single configuration file with strict validation, `${VAR}` and
  `${file:/path}` references for secrets, and `VAHUB_*` environment overrides
  for container deployments.
* A CLI covering `init`, `doctor` and module management.
* A container image and a systemd unit for running the hub as a service.

### Security

* The gate is enforced in code on the path that the agent, the scheduler and a
  confirmed destructive action all share. A manifest cannot grant its own module
  permission; only `vahub.yaml` authorizes.
* A module process receives only the environment variables its manifest
  declares, never the hub's environment, so one module's credential is not
  readable by another.
* Modules are spawned from an argv list, never through a shell, and can be
  dropped to their own uid with the supplementary groups of the parent cleared.
* A tool call that times out is discarded rather than matched to the next
  caller, so a late response from a slow module cannot be returned as another
  call's result.
* Untrusted module output is guarded rather than trusted: a result that is not
  an object is an error, not an exception, and module controlled strings are
  inserted into the page as text and never as markup.
* Arguments listed under a manifest's `audit.redact` are masked before the audit
  record is written.
* The web interface binds to loopback by default, checks the browser origin on
  requests and on WebSocket upgrades, and treats the authentication header from
  a reverse proxy as an audit field rather than as an authorization input.

[Unreleased]: https://github.com/LynnDelpy/vahub/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/LynnDelpy/vahub/releases/tag/v0.1.0
