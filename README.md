# vahub

vahub is a self-hosted voice assistant hub: you speak or type, a language model decides which tool to
call, a policy gate written in code authorizes the call, and a module carries the action out. It is for
people who want an assistant that can actually do things in their home, without giving a model
unrestricted control of it.

The gate is the point of the project. Everything else exists so that the gate has something to authorize.

## The one path

Every action follows the same path, whether it started as speech, as typed text, or as a cron routine.

```
   speech or text                              scheduler
        |                                    (cron routines)
        v                                          |
  +----------------+                               |
  | the assistant  |                               |
  | POST /api/chat |                               |
  +--------+-------+                               |
           |                                       |
           v                                       |
  +----------------+     +---------+               |
  |   agent loop   |<--->|  model  |               |
  +--------+-------+     +---------+               |
           |  tool call                            |  routine steps
           v                                       v
  +-------------------------------------------------------------+
  |                        policy gate                           |
  |          default deny, every call, every argument            |
  +-----------+-------------------------------------+-----------+
              | allow                               | confirm
              v                                     v
  +--------------------+                    pending confirmation
  |     module API     |                    (a human confirms it,
  +---------+----------+                     arguments frozen)
            |  MCP over stdio, one call in flight per module
   +--------+---------------+----------------+
   v                        v                v
+--------+            +-----------+     +----------+
|  time  |            |   home    |     |  notify  |   separate processes,
+--------+            +-----------+     +----------+   minimal environment,
                                                       their own uid
```

The model never talks to a module. It emits a tool call, the gate decides, and only then does the module
API dispatch it. Denied calls come back to the model as an ordinary tool result saying it was denied, so
the assistant can tell you plainly instead of pretending it worked. Every call, allowed or denied, lands
in a SQLite audit log with the principal that made it.

## Quickstart

Requires Python 3.12 or newer.

```bash
git clone https://github.com/LynnDelpy/vahub
cd vahub
python -m venv .venv && . .venv/bin/activate
pip install -e .
```

Create a configuration:

```bash
vahub init
```

That writes a starter `vahub.yaml` next to you: web server on loopback, `policy.default: deny`, and
`llm.provider: mock`, so a fresh install starts, needs no credentials, and can do nothing dangerous.
Read it, then point the `llm` section at a real model. Every setting is documented in
[docs/configuration.md](docs/configuration.md).

Add a module. The catalog is an index, not a store: it maps a short name to a pinned source, and
installing pulls the code from wherever its author keeps it.

```bash
vahub module search home        # browse the catalog
vahub module add time           # no configuration needed
vahub module add homeassistant  # prints the configuration keys it requires
vahub module list
```

A module only ever sees the environment variables its manifest declares, so a token belonging to one
module is not readable by another. Record them at install time with `--set`, or put them in the
environment the hub itself runs in (an `EnvironmentFile` in the systemd unit, or an export in your shell
for a first run):

```bash
export HA_URL=http://homeassistant.lan:8123
export HA_TOKEN=...
```

Check the configuration and the installed manifests before you start:

```bash
vahub doctor
```

Then run the hub and open <http://127.0.0.1:8080>.

```bash
vahub run
```

The page is the assistant and nothing else: a text box, a microphone button, and a panel that appears
when a destructive action is waiting for you to confirm it. It is meant to be handed to whoever lives
in the house. Module state, stderr and the audit log are not on the web; they are read with the CLI on
the host (`vahub doctor`, `vahub audit`). There is no login: the hub binds to loopback by default, and
putting it anywhere else means putting an authenticating reverse proxy in front of it. See
[docs/security.md](docs/security.md).

A fresh `vahub.yaml` has `policy.default: deny` and no rules, so the assistant can talk but cannot do
anything yet. The next section is how you change that.

## A conversation, and what it did

```
you    turn the kitchen light down to 30 percent
vahub  Kitchen light set to 30 percent.
```

What happened underneath, as `/api/chat` reports it:

```json
{
  "session_id": "8f3c...",
  "reply": "Kitchen light set to 30 percent.",
  "steps": [
    {
      "tool": "homeassistant__light_turn_on",
      "args": { "entity_id": "light.kitchen", "brightness_pct": 30 },
      "result": { "ok": true, "result": { "entity_id": "light.kitchen", "state": "on" } }
    }
  ]
}
```

The model picked the tool and the arguments. The gate checked that `homeassistant.light_turn_on` has a
rule, that `entity_id` matched the pattern in that rule, and that `brightness_pct` was inside the
allowed range. The module then made one HTTP call to Home Assistant. The audit log row for this reads
`principal=agent decision=allow result=ok`.

Now the same conversation against a lock:

```
you    unlock the front door
vahub  That needs confirmation. I have queued it; confirm it on the page.
```

`lock_unlock` is classed `destructive`, so the gate does not dispatch it. It stores the call with its
arguments frozen, publishes a confirmation request, and returns
`{"ok": false, "error": "confirmation_required", "pending_id": "..."}` to the model. A human presses
Confirm on the display, within `policy.confirm_ttl_s`, and the frozen arguments are executed. Nothing
the conversation says afterwards can change which door gets unlocked, and the model cannot confirm its
own request.

## The policy gate

A tool-level allowlist is not enough. Most integrations hand you one credential that is admin or
nothing, so the interesting question is never "may the assistant call `light_turn_on`", it is "on
which entity, and with what value". The gate therefore checks arguments, and it denies any argument the
rule does not describe.

Rules live in `vahub.yaml`:

```yaml
policy:
  default: deny
  confirm_ttl_s: 60

  principals:
    agent:     { confirm: [destructive] }
    scheduler: { confirm: [], deny: ["*.lock_*", "*.unlock_*"] }

  rules:
    homeassistant.light_turn_on:
      class: write
      constraints:
        entity_id:      { matches: "^light\\.(kitchen|bedroom|hall)$" }
        brightness_pct: { range: [1, 100] }
```

Before, with only the tool name allowed, this call goes through:

```json
{ "tool": "homeassistant.light_turn_on",
  "args": { "entity_id": "light.greenhouse_uv", "brightness_pct": 100 } }
```

After, with the rule above, it does not:

```json
{ "ok": false,
  "error": "policy_denied",
  "detail": "argument entity_id: 'light.greenhouse_uv' does not match '^light\\.(kitchen|bedroom|hall)$'" }
```

The same rule denies `{"entity_id": "light.kitchen", "transition": 0}`, because `transition` has no
constraint entry. An argument the rule was not told about is refused rather than passed along, which is
what keeps a rule from quietly aging into a hole as a module grows new parameters.

Rules also shape what the model sees. A tool with no rule for the acting principal is left out of the
catalog entirely, so the model does not plan calls that would only die at the gate.

The gate is checked in one place, on the path that the agent, the scheduler, the confirmation flow and
the development endpoint all share. It is not advice in a prompt, and it is not something a module can
grant itself: a manifest declares what a module offers, `vahub.yaml` decides what may be called.

## Modules

A module is a separate program that speaks MCP over stdin and stdout. The hub spawns it, talks to it
over a pipe, and can kill it. It is never imported, so a module can be written by someone else, in any
language, and still be safe to run and easy to reason about. Each one gets only the environment
variables its manifest names, so a token belonging to one module is not readable by another.

* Catalog: <https://github.com/LynnDelpy/vahub-modules>
* Writing your own: [docs/writing-modules.md](docs/writing-modules.md)

Installing a module runs its author's code on your machine. Sources are always pinned to a tag or a
commit, and a third-party source can be installed without any registry at all:

```bash
vahub module add --source git+https://example.com/mod.git@v1.2.3#subdir=modules/foo
vahub module add --source ./my-module
```

## Documentation

| Document | What is in it |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Components, the module contract, the event bus, data flows, non-goals |
| [docs/configuration.md](docs/configuration.md) | Every section of `vahub.yaml`, precedence, secrets, environment overrides |
| [docs/security.md](docs/security.md) | The security model and how to deploy it |
| [docs/writing-modules.md](docs/writing-modules.md) | Building a module against the contract |
| [docs/cli.md](docs/cli.md) | Every command and option |

An annotated configuration is in [examples/vahub.yaml](examples/vahub.yaml). Deployment material
(systemd units, container files) is under [deploy/](deploy).

## What is not solved yet

Read this before you point it at anything that matters.

* **The hub has no authentication.** It binds to loopback by default. Exposing it to a network means
  putting a proxy in front that does TLS and authenticates clients. The subject the proxy passes is
  recorded in the audit log; it is not an authorization input.
* **Module isolation is a process boundary, a uid and a stripped environment. It is not a sandbox.**
  There are no namespaces and no seccomp filter. A module you install can do anything that uid can do.
* **Principals are roles, not accounts.** `agent`, `scheduler` and a confirming human are different
  principals, but there is no per-person policy and no user database.
* **Policy rules are written by hand.** Installing a module does not generate rules for it, and there is
  no editor for them on the web. This is deliberate for now: the file is the boundary, so it should
  be read and understood, not clicked together.
* **Conversation memory is shallow.** The working context of a session lives in memory and is trimmed to
  the recent turns; it resets when the hub restarts. Messages are persisted for the record, not replayed
  into the model.
* **Replies are not streamed.** A turn returns when it is finished.
* **No wake word.** Voice input starts when you press the button. Speech recognition is either the
  browser's own or an OpenAI-compatible endpoint you configure; no local speech models are bundled.
* **Routines are straight step lists.** The scheduler has no conditionals, no branching, and no retries
  beyond aborting the routine when a step fails.
* **One node.** There is no clustering and no failover.

## License

MIT. See [LICENSE](LICENSE).
