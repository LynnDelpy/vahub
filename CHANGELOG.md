# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Until 1.0.0 the configuration file and the module contract may change between
minor versions. Anything that breaks an existing `vahub.yaml`, manifest or
registry entry is listed under Changed with what an operator has to do.

## [Unreleased]

### Added

* **Account roles: admin and user.** Every account used to have identical rights,
  which is fine for one person and wrong for a household: whoever could sign in
  could install a module and hand it a token. An **admin** may install, configure
  and remove apps and manage accounts; a **user** may talk to the assistant,
  arrange the dashboard, approve a held-back action, and edit the places and
  schedules the household shares. The role is read from the account on every
  request, so a demotion or a disable takes effect on the next request rather
  than when a cookie expires. Neither role can edit the policy: that is still
  `vahub.yaml`. With `web.auth.enabled` off there are no accounts and no roles,
  and everyone the proxy lets in is an operator, exactly as before.
* **Managing accounts from the browser.** An admin can add, block, promote,
  demote, reset and remove accounts under their name in the sidebar; anyone can
  change their own password (which requires the current one, and ends every other
  session). `vahub user` still does all of it on the host and remains the way
  back in. The hub refuses the web change that would leave no admin able to sign
  in; the CLI warns and obeys, because it is the recovery path.
* **A sidebar you can collapse, with your apps in it.** Apps moved out of
  Settings and into the sidebar, one entry each with a state dot, and each app
  now has a page of its own (what it is, what it can do, the details it needs,
  and a one-tap way to put a read-only tool on Home). The sidebar collapses to a
  strip of icons; the choice is remembered in the browser, not in the hub's
  shared settings. Your name sits at the bottom and opens everything about you
  and the household: places, automations, preferences, your password, and People.
* **Built-in accounts and login.** The hub can require its own sign in instead of
  relying only on a reverse proxy. Named accounts (scrypt-hashed passwords,
  revocable DB sessions in an HttpOnly SameSite=Strict cookie) are managed with
  `vahub user add/list/passwd/role/disable/enable/remove`; the hub never sets a
  password itself. `web.auth.enabled` defaults on, so a browser-reachable hub is
  not open. The audit log records which account confirmed an action.
* **Saved data the owner and the assistant can edit.** Locations (home, work),
  key/value preferences, and a memory the assistant can write to, all in the
  database. A built-in `core` module offers gated tools (set_location, remember,
  create_schedule, ...) so the agent can manage them through the same policy gate
  as any module; a signed-in owner edits the same data through origin-checked
  REST routes and the web UI. Policy rules stay file-only; accounts are managed
  by an admin or with `vahub user`.
* **Runtime-editable schedules.** Cron routines can be created and removed at
  runtime (by the UI or the assistant) and are persisted. They still run as
  principal `scheduler`, so they are bounded by the scheduler's policy at run
  time regardless of who created them. File schedules stay read-only.
* **A real web UI.** A collapsible sidebar (Home, Chat, your apps), with places,
  automations, preferences and people behind your name, all behind the login.
  Still rendered without `innerHTML`; the CSP nonce is bound to the inline tags.
* **New modules** (in vahub-modules): `weather` (Open-Meteo, no key) and
  `calculator` (safe arithmetic, no eval, no network).

### Fixed

* **Signing out now takes the conversation with it.** The transcript stayed on
  screen and the browser kept its `session_id`, so on a shared tablet the next
  person saw the previous account's bubbles and their first message was appended
  to that account's history (a conversation is not bound to who created it). The
  thread, the session id, and the cached app/settings panels are all cleared on
  sign-out and on a session that expires mid-use.

### Changed

* Store schema v4 adds `users.role`. Accounts that already exist become admins:
  they were created under the old rule, where signing in meant full rights, so
  demoting them silently would take away something the operator had already
  granted. Narrow them down deliberately with `vahub user role <name> user`.
* `vahub user add` creates a plain user; pass `--admin` for an admin. The first
  account on a hub is an admin whatever you pass. `vahub user list` shows the
  role, and `vahub user role <name> <admin|user>` changes it.
* `GET /api/modules` returns a trimmed view to a non-admin (name, version,
  description, state, tools) and carries `can_manage`. Configuration keys, the
  last error and `has_policy_rule` are admin-only, as is
  `GET /api/modules/available` and every module write.
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
