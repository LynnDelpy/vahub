# Security model

An assistant that can act is a program that turns fuzzy natural language into privileged operations.
The interesting question is not whether the model is well behaved. It is what happens when it is not:
when it misreads a request, when a web page it summarised contains instructions, or when a module it
called returns text designed to steer it.

This document states what is enforced, what is merely helpful, and what is not defended at all.

## What this protects against

* A model that is wrong, or that has been talked into something by content it processed.
* A module that is buggy, hostile, or compromised, returning whatever it likes to the hub.
* A page in a browser on your network trying to drive the hub's API.
* A credential that is broader than the task needs, which is nearly all of them.

## The gate is the only real boundary

Every tool call passes through one function: the module API, with the policy gate in front of it. The
agent, the scheduler and the confirmation flow all use that path. There is no
second way to reach a module, because a second way would mean there is no boundary, only a habit.

The gate is default deny. A tool with no rule is refused. It evaluates:

1. Is the module known, ready, and does it actually offer this tool?
2. Is there a rule for `module.tool`? If not, deny.
3. Does the acting principal deny this tool by pattern? If so, deny.
4. Does every argument in the call have a constraint entry, and does every value satisfy it? If not,
   deny.
5. Is the tool's class one this principal must confirm? If so, hold it for confirmation.
6. Otherwise, allow.

Denials are not exceptions. They return to the caller as an ordinary structured result, which means the
model sees `{"ok": false, "error": "policy_denied", "detail": "..."}` and can tell you it was not
permitted, rather than inventing an outcome.

## Why the prompt is not a boundary

The system prompt tells the model that tool results are data and never instructions. That is worth
saying, and it is not a control. Prompts are advisory by construction: they are text in the same channel
as the attack.

Three consequences shape the design:

* **Tool results are untrusted input.** They enter the context as tool-role data and are truncated to a
  fixed size, but nothing stops a model from being influenced by them. The only reason that is
  acceptable is that whatever the model decides to do next still has to pass the gate.
* **Catalog filtering is ergonomics, not security.** Tools that the acting principal could never call
  are hidden from the model so it does not waste turns planning them. Hiding a tool is not what stops
  the call; the gate is, and it re-checks on every call regardless of what the model was shown.
* **A module's self-description is a claim.** The manifest's `tools` block, with its class per tool, is
  advisory. A module cannot grant itself permission by describing itself generously. `vahub.yaml`
  decides.

## Argument-level constraints

Tool-level allowlists are the common design and they are not sufficient. Most integrations issue one
credential that is admin or nothing: a home automation long-lived token can drive every entity in the
house. Given such a token, "may the assistant call `light_turn_on`" is the wrong question. The question
is which entity, and with what value.

```yaml
homeassistant.light_turn_on:
  class: write
  constraints:
    entity_id:      { matches: "^light\\.(kitchen|bedroom|hall)$" }
    brightness_pct: { range: [1, 100] }
```

Two properties matter here.

**Unlisted arguments are denied.** A call carrying `transition: 0` is refused, because the rule says
nothing about `transition`. The alternative, passing through what the rule did not mention, means every
new parameter a module gains is silently permitted from the day it ships. This is the single most
important detail of the gate and the one most likely to be described as inconvenient.

**Constraints are checked before dispatch, on the exact values that will be sent.** There is no later
step that could rewrite them.

The available constraints are `in`, `matches`, `range` and `max_len`, and they compose. `matches` is a
full match (re.fullmatch): the pattern must describe the whole value, so `light\.[a-z_]+` allows
`light.kitchen` but not `xlight.kitchen` or a trailing anything, and you do not need `^`/`$`. Patterns
are compiled when the config loads, so a broken regex stops startup instead of failing open or failing
late.

Write rules narrowly and widen them when something is refused. A denial is recorded in the audit log
with the reason (`vahub audit --denied`), and the assistant reports the refusal in the reply, so
widening is a small edit and a restart.

## Classes and the confirmation flow

Every rule has a class: `read`, `write`, or `destructive`. A class only means something in combination
with a principal, which declares which classes it must have confirmed:

```yaml
principals:
  agent:     { confirm: [destructive] }
  scheduler: { confirm: [], deny: ["*lock*", "*unlock*", "*delete*"] }
```

When the agent proposes a destructive call, the gate does not run it. It:

1. Stores the call with its arguments **frozen**, and an expiry of `policy.confirm_ttl_s`.
2. Publishes a confirmation request, which the assistant page shows as a prompt with the module, the
   tool and the reason.
3. Returns a `pending_id` to the model, which reports that confirmation is needed.

A human then confirms on the display. The stored arguments are executed, not whatever the conversation
has since drifted to, and the confirming subject is recorded as the principal for that call. Confirming
twice does nothing; an expired confirmation is refused and has to be proposed again.

Three rules follow from this and are worth being explicit about:

* **The model cannot confirm its own request.** Confirmation arrives through a different channel than
  the one the request came from.
* **Voice alone never completes a destructive action.** Speech is the least reliable channel for
  identity and the easiest to trigger from outside a window. Confirmation goes to a display.
* **Freezing is the point.** Without frozen arguments, "unlock the front door" could be confirmed and
  then executed against a different lock, and the audit log would faithfully record the wrong thing.

## Principals

A principal is a role, not a person: `agent`, `scheduler`, and the
subject that confirms a pending call. They exist because the same tool carries different risk depending
on who is calling.

The scheduler may act unattended, at 06:30, with nobody watching. That is exactly why it is denied
locks by glob rather than trusted not to touch them. The agent is the opposite: interactive, and
therefore able to ask, so it requires confirmation for destructive classes rather than being denied
outright.

Principals are not accounts. There is no per-person policy and no user database. If you need one, the
authenticating proxy in front of the hub is where identity lives.

## Module isolation, and its limits

A module is a separate process speaking MCP over a pipe. The hub enforces:

* **argv, never a shell.** Commands are argument lists. There is no shell, no interpolation, no word
  splitting, and nothing for a crafted value to escape into.
* **A minimal environment.** A module receives only the variables its manifest declares, plus `PATH`,
  `HOME` and `LANG`. The hub's environment is not passed through. Each declared key is resolved per
  module first as `VAHUB_MOD_<NAME>_<KEY>`, so a secret provided that way reaches only its own module
  even if another module names the same key; a bare `<KEY>` still works but is shared across any module
  that declares it, and the supervisor logs when one is used.
* **Privilege drop where possible.** If the manifest names a `user` and the hub runs as root, the child
  drops privileges before exec with `initgroups`, `setgid`, then `setuid`, in that order. Clearing
  supplementary groups first matters: without `initgroups`, the child keeps every group root belonged
  to, including group 0 and device groups.
* **Pinned installs.** A module source is a tag or a commit, never a moving branch, so an install is
  reproducible and an update is a decision.
* **Bounded interaction.** Every call has a timeout, one call is in flight per module at a time, health
  probes take the same lock, and a module that floods stdout past the read buffer is treated as broken
  rather than being allowed to consume memory.
* **No capabilities offered to the module.** The MCP client announces nothing: no `roots`, no
  `sampling`. A module cannot ask the hub to run the language model on its behalf, because
  server-to-client requests are refused at the protocol level.

Everything a module returns is treated as untrusted. Results that are not objects, error flags, oversize
payloads and malformed JSON-RPC frames are all handled without raising into the caller. The assistant
page renders module-controlled strings (the module, tool and argument names quoted in a confirmation
prompt) as text, never as markup, because a page that renders them is exactly where stored cross-site
scripting would live.

The limits, stated plainly:

* This is **not a sandbox**. There are no namespaces, no seccomp filter, no cgroup limits, and no
  filesystem restriction beyond ordinary Unix permissions. A module can read anything its uid can read
  and connect anywhere the host can connect.
* **Installing a module runs its author's code**, at install time and afterwards. Pinning makes it
  reproducible; it does not make it safe. Read what you install, or run it as a uid that cannot hurt
  you.
* Privilege drop needs the hub to start as root. In a container that runs unprivileged, the `user` field
  is a no-op and every module shares the hub's uid.
* A module can still exhaust CPU or disk. Use the host's cgroup and quota mechanisms if that matters.

## The audit log

Every call is recorded in SQLite before and after the gate's decision, with the timestamp, the
principal, the module, the tool, the arguments, the decision (`allow`, `deny`, `confirm`,
`allow-confirmed`), the result, and the duration. Denied calls are recorded too, which is what makes
"why did nothing happen" answerable, and what makes an attempt pattern visible.

Arguments are redacted using the `audit.redact` list in the module's manifest, so a token passed as a
tool argument is stored as `***`.

Recording never breaks the call path: an audit failure is swallowed rather than turning a working action
into an error. That is a deliberate availability choice with a cost, so treat the log as a very good
record rather than a guaranteed complete one, and keep the database on storage that is not full.

The log is read with `vahub audit` on the host and, since it is SQLite, directly:

```sql
SELECT datetime(ts,'unixepoch','localtime'), principal, module, tool, args, decision, result
FROM tool_calls ORDER BY id DESC LIMIT 50;
```

Module state transitions are mirrored into the database as well, so a post-mortem after a restart still
shows what happened.

## The web surface

The hub authenticates with a built-in login or a reverse proxy. `web.auth.enabled` is on by default: a
named account (created with `vahub user add`, scrypt-hashed password, a revocable database session in an
HttpOnly SameSite=Strict cookie) is required to reach anything but the page shell, the health probes and
the login itself. Turn it off only when a proxy in front already authenticates; the hub never invents a
credential for you. Whoever confirms an action is recorded in the audit log by account.

What the hub does either way:

* Binds to `127.0.0.1` by default.
* Checks an `Origin` allowlist on state-changing routes and on the event WebSocket. Same-origin does not
  prevent another page from sending a cross-origin POST, and it does not apply to WebSockets at all. A
  request with no `Origin` (a script, curl) is allowed, which is why the bind address matters.
* Exposes the assistant and a signed-in owner's own settings (saved places, preferences, schedules), and
  nothing operator-facing: module states, module stderr, the audit log and direct tool invocation are
  not reachable over HTTP at all. They are read with the CLI on the host (`vahub audit`, `vahub doctor`,
  `vahub module verify`). The owner's own settings and the assistant's `core.*` tools edit saved data
  only; the policy and the accounts stay file/CLI-only, so neither the UI nor the model can widen what
  the assistant is allowed to do.
* Records the subject from `web.auth_subject_header` for the audit log. It is never an authorization
  input, because a header is trivially forged by anything that can reach the port directly.

## Deploying it

### Put a proxy in front

Do not expose the hub. Terminate TLS in a reverse proxy, authenticate there, and let only the proxy
reach the hub's port.

```
browser --TLS + client cert--> proxy --plain HTTP on loopback--> vahub
```

Client certificates (mTLS) suit this workload better than passwords: the clients are your own devices,
there are few of them, and there is no signup flow to build. Issue one certificate per device, keep the
CA key offline, and be able to revoke one device without touching the others. An OIDC or forward-auth
proxy is a reasonable alternative if you already run one.

The proxy should pass the verified subject in the header named by `web.auth_subject_header` so the audit
log records who confirmed an action. Strip that header from inbound requests at the proxy, so a client
cannot set it.

### Restrict egress

The hub reaches the language model endpoint. Each module reaches its own backend and nothing else. That
is a narrow enough profile to enforce, and it is the difference between a compromised module being an
annoyance and being a data exfiltration path.

Running modules as separate uids makes per-uid firewall rules possible (`iptables -m owner --uid-owner`,
or the nftables equivalent). If everything runs as one uid, that lever is gone, which is a reason to set
`runtime.user` in production even though nothing else requires it.

### Harden the unit

Run the hub as its own user, with `state_dir` and `modules_dir` owned by it and not writable by anyone
else. The manifest directory deserves attention: whoever can write a manifest chooses what the hub
executes.

The usual systemd options apply and are cheap: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`
with `StateDirectory`, `ProtectHome`, `RestrictAddressFamilies`, `MemoryMax`. Note that privilege drop
for modules requires the hub to start as root, so `NoNewPrivileges` and dropping to a service user are
alternatives to per-module uids, not companions. Pick one deliberately. Unit files are under
[deploy/systemd](../deploy/systemd).

### Handle secrets as files

Prefer `${file:...}` references over environment variables where the platform supports it. systemd
credentials, Docker secrets and Kubernetes secrets all present a secret as a file, and a file does not
show up in `/proc/<pid>/environ` or in a crash dump of the parent.

```yaml
llm:
  api_key: ${file:/run/credentials/vahub.service/llm_key}
```

Module credentials are environment variables by contract, so give those to the hub through an
`EnvironmentFile` with mode 0600 owned by root. Only the keys a module's manifest declares reach it.

### Back up the database, and read it

`<state_dir>/vahub.db` holds the audit log, pending confirmations and conversation history. It is the
only record of what the assistant did. Back it up with `sqlite3 .backup` or a filesystem snapshot, not
by copying a live WAL database.

Reading it occasionally is worth more than any amount of configuration. Denials cluster around rules
that are wrong, and repeated confirmations for the same action mean either the class is wrong or someone
is being trained to click Confirm without reading it.

## What is not defended

* **A compromised host.** Everything above assumes the machine is yours.
* **A hostile module, after you install it.** Pinning and uid separation limit the blast radius. They do
  not make arbitrary code safe.
* **The language model provider.** Conversation text and tool descriptions go to whatever endpoint you
  configure. Run a local model if that matters.
* **Physical access to an unlocked console.** The confirmation flow assumes the person at the display is
  the person you meant.
* **Traffic between the proxy and the hub**, if you put them on different machines. Keep them on the
  same host, or secure that hop yourself.
