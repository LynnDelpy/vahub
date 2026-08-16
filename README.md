# vahub

A self-hosted voice assistant hub. You speak or type, a language model decides which tool to call, a
policy gate written in code authorizes the call, and a module carries it out. It is for people who want
an assistant that can act in their home without giving a model unrestricted control of it.

The gate is the point of the project. Everything else exists so the gate has something to authorize.

## The one path

Every action, whether it started as speech, typed text, or a cron routine, takes the same path.

```
  speech / text / cron
          |
          v
  +----------------+     +---------+
  |   agent loop   |<--->|  model  |
  +--------+-------+     +---------+
           |  tool call
           v
  +-------------------------------------+
  |             policy gate             |   default deny, every call, every argument
  +----------+--------------+-----------+
             | allow        | confirm
             v              v
       module API     pending confirmation (a human approves; arguments frozen)
             |  MCP over stdio, one call in flight per module
             v
   separate module processes (minimal env, own uid)
```

The model never talks to a module. It emits a tool call, the gate decides, and only then is it
dispatched. A denied call comes back as an ordinary tool result, so the assistant says so plainly
instead of pretending it worked. Every call, allowed or denied, lands in a SQLite audit log.

## Quickstart

Requires Python 3.12+.

```bash
git clone https://github.com/LynnDelpy/vahub && cd vahub
python -m venv .venv && . .venv/bin/activate
pip install -e .
vahub init          # writes a starter vahub.yaml: loopback, policy deny, mock model
vahub user add me   # create your login (asks for a password)
vahub module add time
vahub doctor
vahub run           # open http://127.0.0.1:8080 and sign in
```

A fresh install starts, needs no credentials, and can do nothing dangerous. Point the `llm` section at
a real model, then add rules for what you want the assistant to do.

## The web page

Behind a login (built in, on by default): tabs for **Chat**, **Locations**, **Settings** and
**Schedules**. Chat is a text box, a microphone, and a card that appears when a destructive action needs
your approval. You can save places, set preferences, and create cron routines by hand; the assistant can
do the same through gated tools. Operator concerns (module state, stderr, the audit log) stay on the CLI
(`vahub doctor`, `vahub audit`).

## The policy gate

A tool-level allowlist is not enough: most integrations hand you one credential that is admin or
nothing, so the question is never "may it call `light_turn_on`", it is "on which entity, with what
value". The gate checks arguments, and denies any argument a rule does not describe.

```yaml
policy:
  default: deny
  principals:
    agent:     { confirm: [destructive] }        # the model must ask before anything destructive
    scheduler: { confirm: [], deny: ["*unlock*"] }  # unattended, so never a lock
  rules:
    homeassistant.light_turn_on:
      class: write
      constraints:
        entity_id:      { matches: "light\\.(kitchen|bedroom|hall)" }
        brightness_pct: { range: [1, 100] }
```

With this rule, `light.greenhouse_uv` is refused, and so is a `transition` argument the rule never
mentioned. A tool with no rule for the acting principal is not even offered to the model. The gate is
one code path shared by the agent, the scheduler and confirmations; a manifest cannot grant its own
module permission.

## Modules

A module is a separate program that speaks MCP over stdin and stdout. The hub spawns it, talks over a
pipe, and can kill it; it is never imported. Each gets only the environment variables its manifest
names, scoped per module (`VAHUB_MOD_<name>_<KEY>`), so one module's secret is not readable by another.

```bash
vahub module search home
vahub module add homeassistant                 # from the catalog
vahub module add --source ./my-module          # or any pinned source
```

Catalog and first-party modules (time, weather, calculator, homeassistant, transit, notify, ...):
<https://github.com/LynnDelpy/vahub-modules>.

## Documentation

Full docs live in their own repo: **<https://github.com/LynnDelpy/vahub-docs>** (architecture,
configuration, security, CLI, writing modules, deployment, FAQ). An annotated config is in
[examples/vahub.yaml](examples/vahub.yaml); deployment material is under [deploy/](deploy).

## Known limits

The built-in login or a reverse proxy is the only authentication; module isolation is a process
boundary and a uid, not a sandbox; policy rules are written by hand (the boundary is the file, not a UI);
conversation memory is shallow and resets on restart; replies are not streamed; there is no wake word and
no clustering.

## License

MIT. See [LICENSE](LICENSE).
