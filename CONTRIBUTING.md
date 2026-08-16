# Contributing

Thank you for taking the time. This document covers the development setup, what
the checks expect, how commits and pull requests are organised, and how to
propose a module.

Two things before anything else:

* Security issues do not go in a pull request or a public issue. See
  [SECURITY.md](SECURITY.md).
* A new capability, such as lights, calendars, transit or notifications, is
  almost always a module rather than a change to the hub. The hub stays small on
  purpose: an agent loop, a policy gate, a supervisor, a scheduler, a store and
  a console.

## Development environment

The project uses [uv](https://docs.astral.sh/uv/). Install it, then:

```bash
git clone https://github.com/LynnDelpy/vahub
cd vahub
uv sync --extra dev
uv run pre-commit install
```

`uv sync` creates `.venv`, installs the runtime and development dependencies,
and installs vahub itself in editable mode. Prefix commands with `uv run`, or
activate the environment once with `source .venv/bin/activate`.

Check that the CLI is there, then write a configuration file and start the hub:

```bash
uv run vahub --version
uv run vahub init
uv run vahub doctor
uv run vahub run
```

Python 3.12 is the minimum. CI runs the tests on 3.12 and 3.13, so do not use
anything newer than 3.12 provides.

## Running the checks

The same four commands CI runs:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

`pre-commit` runs the first two automatically, fixes what it can, and adds a few
cheap guards: no private keys, no strings that look like live credentials, no
`shell=True`, no HTML interpolation in the assistant page assets. Run it over
everything once with `uv run pre-commit run --all-files`, and update the pinned
hook versions with `uv run pre-commit autoupdate`.

Tests are in `tests/`, split three ways:

```bash
uv run pytest tests/unit           # fast, no subprocesses
uv run pytest -m integration       # spawns real processes or containers
uv run pytest -m contract          # validates a module against the contract
uv run pytest -m "not integration" # what you usually want while iterating
```

Coverage is reported by CI. Locally:

```bash
uv run pytest --cov=vahub --cov-report=term-missing
```

Line length is 110 and formatting is whatever `ruff format` produces. Do not
argue with the formatter in review.

## What a change should look like

* Full type hints. Async where the surrounding code is async.
* Comments explain why, not what. If a line needs a comment to say what it does,
  the line is the problem.
* A module docstring states the purpose and the one or two decisions a reader
  would otherwise get wrong.
* Untrusted input stays untrusted. Module results are guarded before use, module
  output is never rendered as markup, and subprocesses are argv lists.
* New behaviour comes with a test. A bug fix comes with the test that would have
  caught it.
* No emojis. In documentation, no dashes used as punctuation; rewrite with
  commas, periods or parentheses.

Files under `src/vahub/config/` and `src/vahub/contracts/` are contracts that
other people's modules depend on. Changing them in a way that breaks an existing
manifest, registry entry or configuration file needs a schema version bump and a
migration note in the changelog.

## Commits

Conventional Commits, in the imperative, with an optional scope:

```
feat(policy): allow range constraints on float arguments
fix(supervisor): discard a late MCP response instead of returning it
docs(security): describe the exfiltration case explicitly
test(mcp): cover a module that writes a partial line and stops
refactor(store): fold the pending confirmation queries into one module
build(deps): raise the minimum pydantic to 2.6
ci: run the container smoke test on pull requests
chore: remove an unused helper
```

Types in use: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`,
`chore`. A breaking change gets a `!` after the type and a `BREAKING CHANGE:`
paragraph in the body explaining what an operator has to do.

The subject line stays under 72 characters and says what the change does, not
what you were thinking. Put the reasoning in the body, which is where it will
still be useful in a year.

## Pull requests

* One topic per pull request. A formatting sweep mixed into a behaviour change
  makes the behaviour change unreviewable.
* Fill in the template, including the security section. Answering "none" is
  fine, leaving it blank is not.
* Add an entry under `## [Unreleased]` in `CHANGELOG.md` unless the change has
  no effect anyone outside the repository could notice.
* Update the documentation in the same pull request when configuration, the CLI
  or a contract changes.
* CI must be green. If a check looks wrong, say so in the pull request rather
  than working around it.
* Expect review comments about the security boundary in particular. They are
  about the code, not about you.

Draft pull requests are welcome early. Opening an issue first is a good idea for
anything large, so nobody spends a weekend on a design that was already decided
against.

## Proposing a module

A module is a separate program that speaks MCP over stdin and stdout. It can be
written in any language, it does not have to live in this repository, and it
does not need permission to exist. Publish it and people can install it:

```bash
vahub module add --source git+https://github.com/you/vahub-mod-thing@v0.1.0
```

The registry at [vahub-modules](https://github.com/LynnDelpy/vahub-modules) is
an index, not a store. Being listed there means `vahub module add thing` finds
it, nothing more.

### What a module has to get right

[writing-modules](https://github.com/LynnDelpy/vahub-docs/blob/main/writing-modules.md) walks through a complete module, from the directory
layout to the policy rules that let it be called. What follows is the short
version of what a reviewer will look at.

1. **A manifest.** The format is defined in
   `src/vahub/contracts/manifest.py`. It declares the command as an argv list,
   the configuration keys the process needs, the health and restart settings,
   the keys to redact from the audit log, and the tools it offers.
2. **Least configuration.** Only the variables named under `config` reach the
   process. Ask for the narrowest credential the service supports, and say in
   your documentation what that credential can reach if the service only issues
   admin tokens.
3. **Honest tool classes.** `read` observes. `write` changes something you would
   not mind undoing. `destructive` changes something that is awkward or unsafe
   to undo, which includes anything that unlocks, deletes, spends or sends to
   someone else. The class is advisory (the operator's policy decides), but
   getting it wrong makes every published policy example wrong too.
4. **A `__health` tool.** The hub calls it to tell "the process is alive" from
   "the thing it talks to is reachable". Return `{"ok": false, "detail": "..."}`
   when the backend is down rather than exiting, so a temporary outage degrades
   the module instead of restarting it in a loop.
5. **Arguments validated on both sides.** The gate constrains what the hub
   sends. Your module still validates, because the gate enforces the operator's
   policy, not your invariants.
6. **Clean behaviour under stress.** One call is in flight at a time. Do not
   write to stdout except for MCP frames, put logs on stderr, and never exit on
   a malformed request.
7. **A pinned source.** Tag your releases. The registry refuses a moving branch
   as a source, because installing a module runs its code on someone's machine.

### Getting listed

Open a module request issue with the details, then a pull request against the
vahub-modules repository adding the entry to `registry.json`. An entry needs a
description, a homepage, the tags, and one version pinned to a tag or a commit
sha with its `requires_config` keys.

Listing is a light review, not an audit. It checks that the manifest is valid,
the tool classes are plausible, the source is pinned, the documentation says
what credential the module needs and what that credential can reach, and the
module behaves under `pytest -m contract`. Nobody is certifying that the code is
safe to run. The registry entry says where a module is, not that it is
trustworthy.

## Licence

By contributing you agree that your contribution is licensed under the MIT
licence, the same as the rest of the project.
