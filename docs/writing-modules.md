# Writing a module

A module is how vahub does anything at all. The hub itself has no ability to turn on a
light, read a sensor or send a message. It plans, it authorizes, it records. Every actual
effect on the world happens inside a module.

This guide takes you from an empty directory to a published module. The example is Python
because that is the shortest path, but nothing here is Python specific: a module is a
process that speaks MCP over stdin and stdout, so any language with a JSON library and the
ability to read a line qualifies.

## Contents

1. [What a module is](#what-a-module-is)
2. [A complete worked example](#a-complete-worked-example)
3. [The manifest, field by field](#the-manifest-field-by-field)
4. [Designing tools](#designing-tools)
5. [Config and secrets](#config-and-secrets)
6. [The health tool](#the-health-tool)
7. [What the hub does with your result](#what-the-hub-does-with-your-result)
8. [Testing](#testing)
9. [Publishing](#publishing)
10. [Checklist before you publish](#checklist-before-you-publish)
11. [Writing a module in another language](#writing-a-module-in-another-language)

## What a module is

A module is a separate program. The hub spawns it with `execve` (an argv list, never a
shell string), writes JSON-RPC requests to its stdin, and reads JSON-RPC responses from its
stdout. That is the entire interface.

The hub never imports your code. It shares no memory with it, no interpreter, no dependency
graph. Three things follow from that, and they are the reason the boundary is drawn where
it is.

* **Your dependencies are yours.** Each module gets its own virtualenv. You can pin an old
  `httpx` without breaking the hub or another module.
* **A crash is contained.** If your module segfaults, leaks, hangs or floods stdout, the
  supervisor notices, kills it, and restarts it with backoff. The hub keeps serving. Other
  modules do not notice.
* **A compromise is contained.** Your module receives only the environment variables its
  manifest declares. It cannot read another module's API token, because that token was
  never in its environment. In production it can also run under its own uid.

The hub announces no MCP client capabilities: no `roots`, no `sampling`, no elicitation. A
server to client request is answered with "capability not supported" and nothing else
happens. A module cannot ask the hub to spend language model budget on its behalf, and it
cannot ask the user a question through the hub. If you need input, take it as a tool
argument.

Two consequences of the transport are worth internalising before you write a line of code.

* **stdout belongs to the protocol.** One JSON-RPC message per line, nothing else, ever. A
  stray `print()` corrupts the stream and the hub will treat your module as broken. Log to
  stderr. The hub captures stderr, keeps the last 200 lines per module, and publishes them
  on its event bus, so stderr is a real logging channel, not a black hole.
* **You get one call at a time.** The hub serialises calls per module, health probes
  included. A tool that takes 30 seconds blocks every other tool in the same module for 30
  seconds. Split slow work into a different module, or make it fast.

## A complete worked example

We will build a module called `tasks` that reads and creates tasks in a self hosted task
server. It has two real tools and the required `__health` tool, needs a URL and a token,
and is small enough to read in one sitting.

### 1. The directory

```
vahub-mod-tasks/
├── pyproject.toml
├── module.yaml
├── README.md
└── vahub_mod_tasks/
    ├── __init__.py
    ├── __main__.py
    └── server.py
```

The distribution name convention is `vahub-mod-<name>` and the import package
`vahub_mod_<name>`. Nothing enforces it, but `vahub module add` and every existing module
follow it, so a reader knows what they are looking at.

### 2. `pyproject.toml`

```toml
[project]
name = "vahub-mod-tasks"
version = "0.1.0"
description = "vahub tasks module (MCP server over stdio)"
requires-python = ">=3.12"
dependencies = [
  "mcp>=1.4,<2",
  "httpx>=0.27",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["vahub_mod_tasks"]
```

Depend on the MCP SDK, not on `vahub`. A module that imports the hub has crossed the
boundary the process separation exists to draw, and it will break the first time the hub
refactors an internal.

### 3. The server

`vahub_mod_tasks/server.py`:

```python
"""vahub tasks module: list and create tasks on a self hosted task server.

Two decisions worth stating. The tools are narrow (list, create) rather than a
generic HTTP passthrough, because the policy gate constrains arguments and a
passthrough would make that constraint meaningless. The token is read from a
file when TASKS_TOKEN_FILE is set, so a deployment never has to put it in an
environment variable.
"""

from __future__ import annotations

import os
import time

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get("TASKS_URL", "http://localhost:3456").rstrip("/")


def _token() -> str:
    inline = os.environ.get("TASKS_TOKEN", "").strip()
    if inline:
        return inline
    path = os.environ.get("TASKS_TOKEN_FILE", "").strip()
    if path and os.path.exists(path):
        with open(path) as fh:
            return fh.read().strip()
    return ""


mcp = FastMCP("tasks")

# One client for the process lifetime. The timeout is deliberately shorter than
# the hub's default call timeout, so a slow backend surfaces as this module's
# error rather than as an opaque hub timeout.
_client = httpx.AsyncClient(
    base_url=BASE_URL,
    headers={"Authorization": f"Bearer {_token()}"},
    timeout=8.0,
)


@mcp.tool()
async def list_tasks(project: str | None = None, limit: int = 10) -> list[dict]:
    """List open tasks, soonest due first.

    project: optional project name to filter by, for example "home" or "work".
      Leave it out to search every project.
    limit: how many tasks to return, 1 to 25. Keep it small: long results are
      truncated before the assistant reads them.
    """
    capped = max(1, min(int(limit), 25))
    params: dict[str, object] = {"limit": capped, "done": "false"}
    if project:
        params["project"] = project
    r = await _client.get("/api/v1/tasks", params=params)
    r.raise_for_status()
    return [
        {"id": t["id"], "title": t["title"], "due": t.get("due_date")}
        for t in r.json()[:capped]
    ]


@mcp.tool()
async def add_task(title: str, project: str | None = None, due: str | None = None) -> dict:
    """Create a task.

    title: the task text, at most 200 characters.
    project: optional project name to file it under. Defaults to the inbox.
    due: optional due date as YYYY-MM-DD.
    """
    payload: dict[str, object] = {"title": title[:200]}
    if project:
        payload["project"] = project
    if due:
        payload["due_date"] = due
    r = await _client.put("/api/v1/tasks", json=payload)
    r.raise_for_status()
    created = r.json()
    return {"id": created["id"], "title": created["title"]}


@mcp.tool(name="__health")
async def health() -> dict:
    """Reserved health probe: is the task server reachable?"""
    t0 = time.monotonic()
    try:
        r = await _client.get("/api/v1/info")
        ok = r.status_code == 200
        return {
            "ok": ok,
            "backend": "reachable" if ok else "error",
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            "detail": None if ok else f"status {r.status_code}",
        }
    except Exception as e:
        return {"ok": False, "backend": "unreachable", "latency_ms": None, "detail": str(e)}


def run() -> None:
    mcp.run(transport="stdio")
```

`vahub_mod_tasks/__main__.py`:

```python
from .server import run

if __name__ == "__main__":
    run()
```

`vahub_mod_tasks/__init__.py` can be empty.

Notice what is not there. No shell. No generic `request(method, path, body)` tool. No
argument that takes a command. No `print()`. The docstrings are written for the model, not
for a colleague, because the model is the only reader that matters at call time: the SDK
turns them into the tool descriptions that go into the prompt.

### 4. The manifest

`module.yaml` in the module's root. This is the file the installer copies into the hub's
`modules.d` directory.

```yaml
schema_version: 1
name: tasks
version: 0.1.0
description: List and create tasks on a self hosted task server
homepage: https://github.com/you/vahub-mod-tasks

runtime:
  command: ["{venv}/bin/python", "-m", "vahub_mod_tasks"]
  cwd: "{state}/modules/tasks"

config:
  required: [TASKS_URL]
  optional: [TASKS_TOKEN, TASKS_TOKEN_FILE]

health:
  interval_s: 30
  timeout_s: 5

restart:
  max_retries: 5
  backoff_base_s: 2
  reset_after_s: 600
  startup_timeout_s: 20

audit:
  redact: [token]

tools:
  list_tasks: { class: read }
  add_task:   { class: write }
```

### 5. Install it

```bash
vahub module verify ./vahub-mod-tasks
vahub module add --source ./vahub-mod-tasks
```

`add` creates a virtualenv for the module, installs it, writes the manifest to the hub's
modules directory (`hub.modules_dir`, `/etc/vahub/modules.d` by default) and tells you
which configuration keys are still missing.

### 6. Allow it in the policy

The module is now installed, and the agent still cannot call it. The `tools` block in your
manifest is a claim about what your module does. It grants nothing. The gate in
`vahub.yaml` is the binding authority, it defaults to deny, and it checks arguments, not
just tool names. An argument with no constraint entry is refused, so every argument a tool
accepts needs a line.

```yaml
policy:
  rules:
    tasks.list_tasks:
      class: read
      constraints:
        project: { max_len: 40 }
        limit: { range: [1, 25] }
    tasks.add_task:
      class: write
      constraints:
        title: { max_len: 200 }
        project: { max_len: 40 }
        due: { matches: "^[0-9]{4}-[0-9]{2}-[0-9]{2}$" }
```

Tools the principal can never call are also hidden from the model's catalog, so a missing
rule shows up as "the assistant does not know it can do that", not as a denial. If your
module seems invisible, check the policy before you check your code.

[configuration.md](configuration.md) has the full policy syntax, and
[security.md](security.md) explains why the gate is shaped this way.

### 7. Try it

Verify the module against the contract before installing it. This spawns it, does the MCP
handshake, checks the reserved `__health` tool and every tool's schema, and confirms stdout
carries only JSON-RPC:

```bash
vahub module verify --source ./vahub-mod-tasks
```

Then install it and ask the assistant. `vahub doctor` reports whether each declared tool has
a policy rule, which is the most common reason a new module appears to do nothing:

```bash
vahub module add --source ./vahub-mod-tasks
vahub doctor
```

A tool is exercised the way it will really be used, through the assistant, so you also see
whether the description reads clearly enough for the model to choose it.

## The manifest, field by field

The manifest is validated strictly: an unknown key is an error, not a silently ignored
typo. The authoritative definition is `src/vahub/contracts/manifest.py`, and
`vahub doctor` reports any manifest that fails to load.

### Top level

| field | type | default | meaning |
|---|---|---|---|
| `schema_version` | int | `1` | Manifest format version. Set it explicitly. |
| `name` | string | required | Module name. `^[a-z][a-z0-9_-]{0,63}$`. It namespaces your tools (`tasks.add_task`) and names the manifest file. |
| `version` | string | `"0.0.0"` | Your module's version. Shown in the UI and used by `vahub module upgrade`. |
| `description` | string | `""` | One line, shown in listings. |
| `homepage` | string | none | Where a user goes to read more or file a bug. |

### `runtime`

| field | type | default | meaning |
|---|---|---|---|
| `command` | list of strings | required | The argv to execute. At least one element. Never a shell string: there is no interpolation, no word splitting, and nothing for a crafted value to escape into. |
| `user` | string | none | Drop to this uid before exec. Effective only when the hub runs as root. See [deployment](deployment.md). |
| `cwd` | string | none | Working directory. Created if missing. Use it if your module writes a cache or a state file. |
| `pythonpath` | string | none | Extra import path. For modules installed from a local checkout during development. Leave it out of a published manifest. |

Three placeholders are expanded when the module is spawned, so one manifest works on
machines with different layouts:

* `{venv}` the virtualenv the installer created for this module,
* `{state}` the hub's `hub.state_dir`,
* `{config}` the directory holding `vahub.yaml`.

### `config`

| field | type | default | meaning |
|---|---|---|---|
| `required` | list of strings | `[]` | Environment variable names that must be present. If any is missing the module is marked `unconfigured` and never spawned. |
| `optional` | list of strings | `[]` | Environment variable names passed through if they happen to be set. |

Names here are the only keys your process receives. See [Config and secrets](#config-and-secrets).

### `health`

| field | type | default | meaning |
|---|---|---|---|
| `interval_s` | float > 0 | `30` | How often the hub calls `__health`. |
| `timeout_s` | float > 0 | `5` | How long it waits for the answer. |

Pick an interval your backend can absorb forever. A probe every 30 seconds is 2880 requests
a day against someone's home server.

### `restart`

| field | type | default | meaning |
|---|---|---|---|
| `max_retries` | int >= 0 | `5` | Restart attempts before the module is marked `failed` and left alone. |
| `backoff_base_s` | float > 1 | `2` | Delay is `base ** attempt`, capped at attempt 6. |
| `reset_after_s` | float > 0 | `600` | A module that ran cleanly this long has its failure count forgiven. Without it, five unrelated blips over a year add up to a dead module. |
| `startup_timeout_s` | float > 0 | `20` | Budget for spawn, MCP `initialize` and `tools/list`. Exceeding it counts as a failed start. |

If your module opens a network connection at import time, keep it inside the startup
budget or move it into `__health`, which is allowed to report a backend as unreachable.

### `audit`

| field | type | default | meaning |
|---|---|---|---|
| `redact` | list of strings | `[]` | Argument names whose values are replaced with `***` in the audit log. |

If any tool takes something secret as an argument (a token, a password, a one time code),
list the argument name here. It is cheap and the audit database is long lived.

### `tools`

A map from tool name to a declaration:

```yaml
tools:
  list_tasks:  { class: read }
  add_task:    { class: write, description: Create a task }
  delete_task: { class: destructive }
```

`class` is one of `read`, `write`, `destructive`. Tool names must match
`^[a-zA-Z_][a-zA-Z0-9_]{0,63}$` and must not start with `__`, which is reserved.

The classes mean:

* `read` observes and changes nothing,
* `write` changes something recoverable (a light, a task, a message),
* `destructive` changes something a person would mind being wrong about (a lock, a
  deletion, a payment). By default the agent may propose a destructive call but a human
  confirms it out of band, with the arguments frozen at proposal time.

Declaring a tool `destructive` is a promise you make to the operator. Declaring a
destructive tool `read` does not make it safe; it makes your module dishonest, and the
operator's policy is what actually stops it either way.

## Designing tools

This is the part that decides whether your module is pleasant or miserable to use. The
rules below are short because each of them has one reason.

### Narrow tools beat one generic tool

A tool like `call_service(domain, service, data)` or `request(method, path, body)` is
tempting: one tool, whole API covered. Do not write it.

The policy gate authorizes a call by inspecting its arguments. With a narrow
`light_turn_on(entity_id, brightness_pct)` the operator writes
`entity_id: { matches: "light\\.[a-z_]+" }` and the assistant genuinely cannot touch a lock. With
a generic passthrough the operator can only allow or forbid the entire API, because the
meaning of the call now lives inside a free form `data` blob the gate cannot reason about.
The gate becomes decoration and the module's backend token, which is usually admin or
nothing, becomes the real permission set.

Ten small tools also work better with the model. Each one has a name and a description that
says exactly when to use it, which is a much easier decision than composing a correct
service call from an API reference the model half remembers.

### Return small results

Every tool result is serialised into the conversation. It is truncated at
`budgets.tool_result_bytes` (16 KB by default) and the truncation is a byte cut, so a large
JSON document arrives at the model as invalid JSON ending in `...[truncated]`.

* Return the fields that answer the question, not the upstream API's full object.
* Support a `limit` argument, cap it in code as well as in the policy, and default it low.
* Support a filter argument for anything that can grow without bound. A house with 400
  entities needs `list_entities(domain="sensor")`, otherwise the useful part is truncated
  away.
* Summarise server side when you can. "3 of 112 tasks are overdue" beats 112 objects.

### Describe every argument

The model chooses arguments from your descriptions and nothing else. Write them for a
reader who cannot see your code, your backend or your database.

Say the format (`YYYY-MM-DD`), the units (percent, celsius, minutes), the range (1 to 25),
what happens when it is omitted, and what a typical value looks like. Naming a valid value
in the text is worth more than any amount of prose: it is the difference between the model
guessing an identifier and copying one.

Bad:

```
project: the project
```

Good:

```
project: optional project name to filter by, for example "home" or "work".
  Leave it out to search every project.
```

### Never accept a free form command

No `command`, no `script`, no `query` that is passed to a shell, an interpreter or an
eval. No raw URL that your module will fetch, because that is a server side request forgery
primitive with an LLM holding the keyboard. No path that you will open without joining it
to a fixed root and checking the result.

Everything the model produces is untrusted input, and tool results from other modules are
untrusted input that has already been in the model's context. Web page contents, task
titles and sensor names have all been used to smuggle instructions. Treat every argument as
hostile and validate it in your module, even though the gate validates it too. Defence in
depth is cheap here: the gate protects the operator from the model, and your validation
protects the operator from a policy with a rule that is looser than it looks.

### Keep the shape stable

Argument names appear in the operator's policy file. Renaming `entity_id` to `entity` in a
patch release silently breaks every constraint that mentioned it, and a constraint that no
longer matches an argument name means the argument is refused, so the tool stops working.
Treat argument names as public API. Add optional arguments freely, remove and rename them
in a major version and say so in the release notes.

Return values should be objects with stable keys, not positional tuples or bare strings
that a caller has to parse.

### Be safe to retry

The agent can retry after a timeout, and a scheduled routine can run twice if the machine
was asleep. Where the backend supports it, make creation idempotent (an idempotency key, a
natural unique key, a check before insert). Where it does not, say so in the description so
the operator can class the tool `destructive`.

### Fail loudly and usefully

Return an error the model can act on: `{"error": "not_found", "entity_id": "light.kitchn"}`
is something it can correct. A raised exception becomes an MCP error and the agent is told
the call failed, which is fine, but the model then has nothing to work with. Never return a
made up success. A tool that pretends to have sent a message is worse than a tool that
fails.

## Config and secrets

The hub builds your process a minimal environment. It contains `PATH`, `HOME`, `LANG`, and
exactly the keys listed in `config.required` and `config.optional` that are present in the
hub's own environment. There is no blanket passthrough of `os.environ`. This is why the
`tasks` module cannot read a Home Assistant token: it was never handed one.

Consequences you need to design around:

* **Undeclared keys do not exist.** If you read `os.environ["TASKS_DEBUG"]` and forget to
  declare it, it is empty in production and set on your laptop, which is the worst kind of
  bug. Declare everything you read.
* **A missing required key means you are never started.** The module sits in state
  `unconfigured` with a message naming the missing keys. That is the intended behaviour:
  do not paper over it by defaulting a required credential to an empty string.
* **Optional means optional.** Handle absence, do not crash on it.

### Secrets

Accept both an inline value and a file path, exactly as the example does:

```
TASKS_TOKEN       the value itself
TASKS_TOKEN_FILE  a path to a file containing the value
```

The file form is how a real deployment supplies credentials, because systemd credentials,
Docker secrets and Kubernetes secrets all materialise as files with restrictive
permissions, and a file does not appear in `/proc/<pid>/environ` or in a crash dump of the
hub. Read the file once at startup and strip trailing whitespace.

Two more rules:

* **Never log a secret to stderr.** stderr is captured, kept in a ring buffer, published on
  the event bus and shown in the web UI. Redact before you log, or do not log it.
* **Never return a secret from a tool.** It would go into the conversation, into the model
  provider's logs, and into the audit trail.

## The health tool

Every module must expose a tool named `__health`. It takes no arguments. It never appears
in the catalog offered to the model, and the hub refuses to call any tool whose name starts
with `__` on behalf of the agent, so it is not an attack surface.

It answers one question that a live process cannot answer by existing: is the thing this
module talks to actually reachable?

Return an object:

```json
{"ok": true, "backend": "reachable", "latency_ms": 12.4, "detail": null}
```

* `ok` is the only field the hub interprets. False (or an MCP error, or a timeout, or a
  result that is not an object) moves the module to `degraded`.
* `backend`, `latency_ms` and `detail` are shown to the operator. Put the useful sentence
  in `detail` when `ok` is false: "status 401" tells someone their token expired.

`degraded` is deliberately different from `failed`. A module whose backend is temporarily
down is not restarted in a loop, because restarting it would not bring the backend back.
The module keeps running, the agent is told the module is unavailable, and the next
successful probe returns it to `ready`.

Guidelines:

* Probe cheaply. A version endpoint or a HEAD request, not a full listing.
* Never write anything in a health probe.
* Return within `health.timeout_s`. Give your own request a shorter timeout than that.
* Never raise. Catch everything and report `ok: false` with the reason.
* A module with no backend (pure computation) returns `{"ok": true, "backend": "n/a"}`.

## What the hub does with your result

Useful to know, because it explains several behaviours that look odd from inside a module.

* **One call at a time.** Calls to your module, health probes included, are serialised. Two
  tools of yours never run concurrently.
* **Every call has a timeout, and a timeout is not a cancellation.** If you answer late,
  the hub has already given up. The reply is matched by request id, found to have no
  waiter, and discarded. It is never handed to the next caller. Your process is not killed,
  so if you started something, you own finishing or aborting it.
* **Results are unwrapped, then guarded.** The hub prefers `structuredContent`, otherwise
  it joins the text blocks and parses them as JSON when they clearly are an object or an
  array. A result that is not an object is turned into a `bad_result` error rather than
  being passed on. Nothing your module returns can crash the hub, and nothing it returns is
  trusted to be well formed.
* **`isError` is respected.** An MCP error result becomes `{"ok": false, "error":
  "tool_error", ...}` for the caller.
* **Everything is audited.** Principal, module, tool, arguments (with `audit.redact`
  applied), the gate's decision, the result and the duration go into SQLite. Assume every
  argument your tools accept will be readable months later.
* **The catalog is live.** Your tools appear in the model's catalog only while your module
  is `ready` and only if the policy permits them for that principal. A module that goes
  degraded disappears from the catalog and the agent is told it is unavailable, which is
  why the assistant says "the task server is not reachable" instead of inventing an answer.

## Testing

### `vahub module verify`

Run this before every commit and in CI:

```bash
vahub module verify ./vahub-mod-tasks     # a source directory
vahub module verify tasks                 # an installed module
```

It exercises the module contract end to end, without the agent and without a language
model, and exits non zero on the first failure:

* the manifest parses and validates against the current schema,
* `runtime.command` resolves to something executable,
* the process starts, completes the MCP `initialize` handshake and answers `tools/list`
  within `restart.startup_timeout_s`,
* the tools it actually exposes match the tools the manifest declares, in both directions
  (an undeclared tool and a declared but missing tool are both errors),
* every tool has a description and an input schema,
* no tool name is reserved (`__` prefix) except `__health`,
* `__health` exists, answers within `health.timeout_s`, and returns an object,
* nothing that is not JSON-RPC was written to stdout,
* the module starts with only its declared configuration keys present.

Add `--json` for machine readable output in CI.

### The contract test suite

The hub repository carries a pytest suite marked `contract` under `tests/contract`, which
is where the same checks live and where a few extras are exercised against a deliberately
badly behaved module: a late reply after a timeout must be discarded rather than handed to
the next caller, a non object result must be rejected, and an oversized stdout line must be
treated as a broken connection rather than a crash. Read those tests if you want to know
exactly how the hub will treat your output.

```bash
pytest -m contract
```

### Testing your own logic

Everything above tests the contract, not your behaviour. Test your tool functions as
ordinary functions, with the backend faked at the HTTP layer. You do not need MCP, a
subprocess or the hub to assert that `list_tasks(limit=100)` returns at most 25 items.

### Testing against the running hub

`vahub doctor` reports module state and any manifest or policy problem, including tools
that exist but have no policy rule, which is the most common reason a new module appears to
do nothing. For a live call, run `vahub module verify` or install the module and ask the
assistant to use it.

## Publishing

### Version and tag

Bump `version` in both `pyproject.toml` and `module.yaml`, keep them equal, and tag the
commit. If your repository holds several modules, namespace the tag:

```bash
git tag modules/tasks/v0.1.0
git push origin modules/tasks/v0.1.0
```

A source is always pinned to an immutable revision. The registry model refuses `main`,
`master`, `HEAD` and any `refs/heads/` reference, because "install whatever main happens to
be today" is not something to build a home on.

### Option A: let people install from your repository

No registry involved, nothing to submit, works immediately:

```bash
vahub module add --source git+https://github.com/you/vahub-mod-tasks.git@v0.1.0
```

If the module lives in a subdirectory of a larger repository:

```bash
vahub module add --source 'git+https://github.com/you/modules.git@modules/tasks/v0.1.0#subdir=modules/tasks'
```

A published wheel works too:

```bash
vahub module add --source pypi:vahub-mod-tasks==0.1.0
```

Put the exact command in your README. It is the only documentation most users will read.

### Option B: submit it to the official registry

The registry is a plain JSON index that maps a short name to a source. It is an index, not
a store: your code stays in your repository. Being listed means `vahub module search tasks`
finds it and `vahub module add tasks` installs it.

Open a pull request against `vahub-modules` adding an entry to `registry.json`:

```json
{
  "tasks": {
    "description": "List and create tasks on a self hosted task server",
    "homepage": "https://github.com/you/vahub-mod-tasks",
    "author": "you",
    "tags": ["productivity", "tasks"],
    "latest": "0.1.0",
    "versions": {
      "0.1.0": {
        "source": {
          "type": "git",
          "url": "https://github.com/you/vahub-mod-tasks",
          "rev": "v0.1.0"
        },
        "requires_config": ["TASKS_URL"],
        "optional_config": ["TASKS_TOKEN", "TASKS_TOKEN_FILE"],
        "requires_vahub": "0.1.0",
        "notes": "Needs a task server API token with write scope."
      }
    }
  }
}
```

`requires_config` and `optional_config` are what the installer prompts for, so a fresh
install is usable without reading your source. Keep them in step with the manifest.

Publishing a new version means adding a `versions` entry and moving `latest`. Old entries
stay, so an existing pin keeps resolving.

A registry listing is a convenience, not an endorsement and not a security boundary.
Installing a module runs its code on someone's machine with whatever configuration they
give it. Say plainly in your README what your module talks to, what permissions its
credential needs, and what the worst thing it can do is.

An organisation can run its own index by pointing `--registry` at another URL or a local
file, which is the supported way to distribute internal modules.

## Checklist before you publish

* [ ] `vahub module verify ./your-module` passes.
* [ ] `__health` exists, probes the real backend, never raises, and is fast.
* [ ] Every tool has a description that says when to use it, and every argument has a
      description with format, units, range and default behaviour.
* [ ] No generic passthrough tool, no free form command, no raw URL or path argument.
* [ ] Results are small and bounded. Anything list shaped takes a `limit` that is capped
      in code, and a filter argument.
* [ ] Destructive tools are declared `destructive`, and the README says why.
* [ ] Every environment variable you read is declared in `config`. Nothing else is read.
* [ ] Secrets are accepted as a file path as well as a value, are never logged, and are
      never returned from a tool.
* [ ] `audit.redact` lists any argument that can carry a secret.
* [ ] Nothing is written to stdout except JSON-RPC. Logs go to stderr.
* [ ] `version` matches in `pyproject.toml` and `module.yaml`, and the commit is tagged.
* [ ] The README shows a working `vahub module add --source ...` line and an example
      policy block with real constraints, not `{}`.
* [ ] The README states what the module talks to and what its credential can do.
* [ ] A licence file exists.

## Writing a module in another language

Nothing in the contract is Python. To write a module in Go, Rust, Node or anything else,
implement the small MCP subset the hub uses:

* Read newline delimited JSON-RPC 2.0 from stdin, write it to stdout, one message per line,
  no embedded newlines. Never write anything else to stdout.
* Answer `initialize` with a `serverInfo` object and `capabilities` containing a `tools`
  key. A module that does not announce the tools capability is rejected.
* Send the `notifications/initialized` notification if your SDK expects the handshake, and
  otherwise ignore notifications you receive.
* Answer `tools/list` with each tool's `name`, `description` and `inputSchema` (JSON
  Schema, object typed).
* Answer `tools/call` with a result containing `structuredContent`, or `content` blocks of
  type `text`. Set `isError: true` for a tool level failure.
* Answer a `tools/call` for `__health` as described above.
* Expect server to client requests to be refused: the hub announces no capabilities, so do
  not rely on sampling, roots or elicitation.

Then write a manifest whose `runtime.command` points at your binary. `{venv}` will not be
meaningful for a compiled module, so use an absolute path or `{state}`:

```yaml
runtime:
  command: ["{state}/modules/tasks/bin/tasks-module"]
  cwd: "{state}/modules/tasks"
```

`vahub module verify` applies exactly the same checks regardless of language, so it is the
fastest way to find out whether your implementation is complete.
