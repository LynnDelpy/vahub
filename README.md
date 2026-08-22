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
vahub start         # writes a starter config if none exists, then runs
```

`vahub start` binds loopback with a deny policy and the mock model, so a fresh install starts, needs no
credentials, and can do nothing dangerous. Open `http://127.0.0.1:8080`, create the first account (it
becomes the owner and the first admin), and do the rest from the browser: add modules, give them their
tokens, arrange your dashboard, and add accounts for everyone else in the house. Point the `llm` section
at a real model, then add policy rules for what you want the assistant to do. (`vahub init` plus
`vahub run` is the same thing spread across a wizard and a service manager, if you prefer that.)

## The web page

Behind a login (built in, on by default). A sidebar you can collapse to a strip of icons holds where you
can go: **Home**, a dashboard of cards you arrange; **Chat**, a text box, a microphone and a card that
appears when a destructive action needs your approval; and then your **apps**, one entry each, with a dot
for whether it is working. Each app has a page of its own: what it is, what it can do, and the details it
needs. Your name sits at the bottom of the sidebar, and everything about you and the household hangs off
it: places, automations, preferences, your password, and (for an admin) the people who can sign in.
Dashboard cards read a module's own data by calling its read-only tools directly, which a signed-in
person may do without a policy rule. Operator concerns (stderr, the audit log) stay on the CLI
(`vahub doctor`, `vahub audit`).

### Two kinds of account

An **admin** can install and configure apps and manage accounts. Everyone else can talk to the assistant,
arrange the dashboard, approve a held-back action, and edit the places and automations the household
shares, but cannot install an app, hand one a token, or touch accounts. The first account is an admin;
`vahub user add <name>` makes a plain one and `--admin` makes another admin. The hub will not let you
remove the last admin who can sign in from the web, and `vahub user` on the host is the way back in if
you manage it anyway.

Installing a module from the UI never grants it permission: its tools stay denied until you add a policy
rule in `vahub.yaml`, which is a file-and-CLI action. So the assistant can never install itself a
capability, and the policy is not editable from the web by anyone, of any role.

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

The built-in login or a reverse proxy is the only authentication, and its roles are two (admin and user),
not a permission system; module isolation is a process boundary and a uid, not a sandbox; policy rules
are written by hand (the boundary is the file, not a UI);
conversation memory is shallow and resets on restart; replies are not streamed; there is no wake word and
no clustering.

## The vahub project

Three repositories, one project:

- **[vahub](https://github.com/LynnDelpy/vahub)** (this one). The hub: agent loop, policy gate, supervisor, scheduler, web UI.
- **[vahub-modules](https://github.com/LynnDelpy/vahub-modules)**. The module catalog and first-party modules.
- **[vahub-docs](https://github.com/LynnDelpy/vahub-docs)**. The full documentation.

## License

MIT. See [LICENSE](LICENSE).
