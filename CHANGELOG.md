# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Until 1.0.0 the configuration file and the module contract may change between
minor versions. Anything that breaks an existing `vahub.yaml`, manifest or
registry entry is listed under Changed with what an operator has to do.

## [Unreleased]

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
* A web console with a chat view, module status, the audit trail and the
  confirmation prompts, plus a JSON API and a Prometheus endpoint.
* Speech input and output, either in the browser or through an OpenAI compatible
  transcription and synthesis endpoint.
* A single configuration file with strict validation, `${VAR}` and
  `${file:/path}` references for secrets, and `VAHUB_*` environment overrides
  for container deployments.
* A CLI covering `init`, `doctor` and module management.
* A container image and a systemd unit for running the hub as a service.

### Security

* The gate is enforced in code on the path that the agent, the scheduler and the
  development endpoint all share. A manifest cannot grant its own module
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
  inserted into the console as text and never as markup.
* Arguments listed under a manifest's `audit.redact` are masked before the audit
  record is written.
* The web interface binds to loopback by default, checks the browser origin on
  requests and on WebSocket upgrades, and treats the authentication header from
  a reverse proxy as an audit field rather than as an authorization input.

[Unreleased]: https://github.com/LynnDelpy/vahub/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/LynnDelpy/vahub/releases/tag/v0.1.0
