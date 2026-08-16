# Architecture

This document describes what the hub is made of, why each piece exists as its own piece, and what
happens along the three paths that matter: a spoken command, a scheduled routine, and a status update.

The organising idea is that there is exactly one place where an action becomes real, and everything
funnels through it. The agent, the scheduler and the confirmation flow all
call the same module API, and the policy gate sits in front of it. If that were not true, the gate would
be one boundary among several, which is another way of saying there would be no boundary.

## Components

```
              +--------------------------------------------------+
              |                    runtime                       |
              |  startup order, signal handling, shutdown        |
              +--+-----------+-----------+-----------+-----------+
                 |           |           |           |
        +--------v--+  +-----v-----+  +--v-------+  +v----------+
        |  web API  |  |   agent   |  | scheduler|  | supervisor|
        | REST, WS, |  |   loop    |  |  cron    |  | processes,|
        | console   |  |           |  | routines |  | health    |
        +-----+-----+  +-----+-----+  +----+-----+  +-----+-----+
              |              |             |              |
              |        +-----v-------------v-----+        |
              +------->|      policy gate        |        |
                       +-----------+-------------+        |
                                   |                      |
                             +-----v-----+                |
                             | module API|<---------------+
                             +-----+-----+
                                   |
                          +--------v---------+
                          |   MCP client     |  one per module process
                          +------------------+

        cross-cutting: event bus, SQLite store (audit, pending, budgets),
                       structured logging, Prometheus metrics
```

### Runtime

Owns the process. It builds every component in dependency order, installs the signal handlers, and
performs shutdown in reverse: stop accepting new work, let the web server drain, stop the scheduler,
terminate modules (SIGTERM, then SIGKILL after a grace period), close the database. Shutdown order is
part of the design rather than an afterthought, because a module killed while a call is in flight and a
database closed under a pending write are both ways to lose the audit record of what just happened.

### Configuration

One file, `vahub.yaml`, validated strictly at startup. Unknown keys are errors. A misspelled
`origin_allowlist` that silently became an empty list would be a fail-open default, so the loader
refuses to guess. Policy rules and scheduled routines are part of the same file and the same validation
pass, which means a broken regex in a rule stops the hub at startup instead of at 3am when the rule is
first exercised. See [configuration.md](configuration.md).

### Supervisor

Discovers manifests, spawns each module, runs the handshake, probes health, and restarts on failure. Its
state machine is:

```
unconfigured -> starting -> ready
                   |          |
                   |          v
                   |      degraded -> ready
                   v          |
                failed <------+
                   |
                   v
                stopped
```

`degraded` is deliberately not `failed`. A module whose backend is temporarily unreachable (the home
automation server is rebooting) is working correctly and should keep its process; restarting it would
achieve nothing but churn. `failed` means the process itself will not stay up, and it is terminal once
the restart budget is spent.

The restart budget forgives history: a module that ran cleanly for `restart.reset_after_s` has its
failure count cleared. Without that, five unrelated blips spread over a year eventually add up to a dead
module for no reason a human would accept.

`unconfigured` exists so a module that is installed but missing its credentials shows up in `vahub
doctor` as exactly that, rather than as a spawn failure that a person has to decode.

### MCP client

Speaks JSON-RPC 2.0 over the module's stdin and stdout, newline delimited, one message per line. It owns
the wire protocol and the request id map; the supervisor owns the process. Keeping those apart is what
makes another transport possible later without touching lifecycle logic.

Three decisions in here are load bearing:

* **A timeout is not a cancellation.** When a call times out, the waiter is removed from the pending map.
  If the module answers afterwards, the response finds nothing waiting and is discarded. Handing a late
  response to the next caller is how you get a hallway light that turns on when you asked about the
  living room, and it is close to impossible to debug after the fact.
* **The client announces no capabilities.** No `roots`, no `sampling`. A module cannot ask the hub to
  run a model on its behalf, because a server-to-client request is refused at the protocol level rather
  than being a feature nobody remembered to turn off.
* **One bad message must not end the conversation.** Malformed JSON, a non-object message, an id of the
  wrong type, or a line longer than the read buffer are all handled locally. Only end of file means the
  process is gone.

### Module API and the policy gate

The single call path. For every call, in order: validate that the module exists, is ready, and offers
that tool; ask the gate; then allow, deny, or hold for confirmation; dispatch; audit.

The gate is default deny and checks arguments, not just tool names. An argument with no constraint entry
is refused. Principals separate who is acting: the agent must ask a human before anything destructive,
the scheduler may act unattended but is denied locks entirely. [security.md](security.md) covers the
reasoning in full.

Dispatch takes a per-module lock, so there is exactly one call in flight per module. That is a
simplifying constraint rather than a performance choice: modules are small programs talking to devices,
and serialising them removes a whole class of ordering bug from both the hub and every module ever
written for it. Health probes take the same lock, so a probe never interleaves with a real call.

Results are always structured. A timeout, a protocol error, an exception, or a result that is not an
object all come back as `{"ok": false, "error": ...}`. Nothing a module returns can raise into the
agent loop.

### Registry (the tool catalog)

Aggregates the tools of every `ready` module into one namespaced catalog, `module.tool`. Reserved names
(anything starting with `__`, including the `__health` probe) never appear. The agent-facing view is
intersected with the policy allowlist, so a tool the agent may never call is not offered to the model at
all. That is ergonomics, not security: hiding a tool stops wasted turns, while the gate is what stops
the call.

The catalog also reports which modules are degraded or failed, and the agent is told, so it can say the
lights are unreachable instead of inventing a reason they did not respond.

### Agent loop

Message, model, tool calls, repeat, answer. Bounded by the budget settings: a cap on iterations per
turn, a wall clock deadline, a byte limit on each tool result before it re-enters the context, and token
budgets per turn and per day. The byte limit matters more than the iteration cap. Eight iterations sound
cheap until one unfiltered `list_entities` result on a real home dwarfs the entire conversation.

Tool results enter the context as tool-role data and the system prompt says they are data, never
instructions. That reduces the chance of a module's output steering the model, and it is not a security
control. The gate is the security control.

### Scheduler

Cron routines that run as `principal: scheduler`, through the gate, without the agent and without a
model. No latency, no tokens, no chance of a routine being talked into something. Overlapping runs are
skipped rather than queued, since a routine that fires every minute and takes two must not build a
backlog. A failing step aborts the routine and lands on the event bus. Timezone is explicit so daylight
saving transitions are deterministic.

### Store

SQLite in WAL mode, one writer. It holds conversations and messages, the tool call audit log, pending
confirmations, module state history, and daily token usage. The audit log is the important table: when
a light comes on at night, it says whether the agent or the scheduler did it, with which arguments, and
whether the gate allowed it.

### Web API and the assistant

A small static page that is the assistant and nothing else: a chat box, a microphone button, and the
prompt to confirm a destructive action. REST carries the chat and voice turns; a WebSocket carries one
thing, the `policy.confirmation_required` event, so a held-back action appears without polling. There is
no operator surface on the web: module state, stderr, the tool catalogue and the audit log are read with
the CLI on the host. The hub itself has no authentication; it binds to loopback and expects a proxy in
front for anything else. State-changing routes and the WebSocket both check an `Origin` allowlist,
because same-origin does not prevent another page from sending a cross-origin POST and does not apply to
WebSockets at all.

The page renders the assistant's replies and the module, tool and argument names inside a confirmation
prompt using `textContent`, never `innerHTML`. A confirmation prompt quotes strings that originate in a
module, which is exactly the kind of input that turns a page into stored cross-site scripting.

### Event bus

In-process publish and subscribe, described below.

### Logging and metrics

Structured JSON logs to stdout. Prometheus metrics: module state as a gauge, tool calls counted by
result, tool latency as a histogram per module and tool, and a counter of dropped bus messages per
topic. Latency is measured per stage on purpose, because "voice feels slow" is not actionable until you
can see whether it is speech recognition, the model, or a module.

## The module contract, and why modules are separate processes

A module declares itself in a manifest: how to start it (an argv list, never a shell string), which
configuration keys it needs, its health and restart parameters, which of its arguments must be redacted
from the audit log, and which tools it offers with a class for each. `vahub module add` writes the
manifest; you do not normally hand-edit one.

Two properties of that contract are worth stating plainly:

* The `tools` block is what the module **claims**. It is advisory. A module cannot grant itself
  permission by describing itself generously; `vahub.yaml` decides what may be called.
* Only the keys named in the manifest's `config` block are passed into the process environment. There is
  no blanket pass-through of the hub's environment, so the clock module never sees the home automation
  token.

Modules are separate processes because that boundary buys several things that an in-process plugin API
does not:

* **Fault isolation.** A module that leaks, blocks, or crashes is a process that can be killed and
  restarted. In-process, it would be the hub that leaks, blocks, or crashes.
* **Language independence.** The contract is a protocol on a pipe, so a module can be written in
  anything. That is what makes a third-party catalog realistic.
* **Privilege separation.** A separate process can run as a different uid with a different environment,
  and its network egress can be filtered separately. None of that is expressible for a library loaded
  into your own address space.
* **A real kill switch.** Timeouts mean something when the thing you are waiting for can be terminated.
* **Untrusted output stays untrusted.** Everything a module returns crosses a serialisation boundary and
  is validated on the way in, rather than arriving as a live Python object that the hub already trusts.

The cost is real and worth naming: process spawn, a pipe protocol, and a handshake are more machinery
than a function call, and every module author has to implement `__health`. That is the price of the
properties above.

## The event bus and its backpressure decision

The bus is in-process publish and subscribe with one rule: **`publish()` never blocks and never fails.**
Producers are the supervisor, the gate, the scheduler and the agent, and none of them may be slowed down
by a dashboard that is not reading fast enough.

That forces an explicit choice about what to do when a subscriber's queue is full. An unbounded queue is
not a choice, it is a memory leak waiting for the first slow WebSocket while a module writes stack
traces to stderr. So every topic has a bounded queue and one of two policies:

| Policy | Behaviour on overflow | Used for |
|---|---|---|
| `drop_oldest` | Discard the oldest message, keep the subscriber, count the drop | High volume streams where recency is what matters: `module.log`, `conversation.message` |
| `disconnect_slow` | Drop the **subscriber**, not the message, count it | Streams where a gap is misleading: `module.state_changed`, `tool.called`, `policy.confirmation_required`, `schedule.fired`, `budget.exceeded` |

The reasoning behind `disconnect_slow`: a subscriber that silently missed a `policy.confirmation_required`
event leaves a destructive action waiting with nobody told to confirm it, and a state mirror that missed
a transition records a module as `ready` when it is not. A gap there is misleading, so cutting the socket
is the honest failure: the subscriber reconnects and is correct again. Losing a log line, by contrast, is
unremarkable, so log topics drop instead.

Both policies increment a dropped-messages counter, so the choice is visible in metrics rather than
invisible in behaviour.

## Data flows

### A spoken command

1. The browser captures speech. With `speech.stt.provider: browser` it transcribes locally and posts
   text, so no audio leaves the machine and no credentials are involved. With `openai_compat` it posts
   audio to `/api/voice` and the hub transcribes it.
2. The web API checks the `Origin` header, resolves or creates a session, and hands the text to the
   agent loop.
3. The agent builds the tool catalog for the `agent` principal, filtered by policy, and calls the model
   with the conversation and the catalog.
4. The model returns either an answer (done) or tool calls.
5. Each tool call goes to the module API. The gate evaluates it:
   * **allow**: the module API dispatches under the module lock, with a timeout, and returns a
     structured result.
   * **deny**: no dispatch. The model gets `{"ok": false, "error": "policy_denied", "detail": ...}` and
     can explain the refusal.
   * **confirm**: no dispatch. The call is stored with its arguments frozen, a
     `policy.confirmation_required` event goes on the bus, and the model gets a `pending_id`.
6. The result is truncated to `budgets.tool_result_bytes` and appended to the context as tool-role data.
7. Loop until the model answers, or a budget stops the turn. Every outcome produces a reply; a turn that
   hit a limit says so.
8. The reply goes back to the browser, which speaks it (locally, or via the configured endpoint).
9. Every dispatched call was written to the audit log along the way, with arguments redacted per the
   module's manifest.

A confirmation is completed out of band: a human presses Confirm within `policy.confirm_ttl_s`, and the
hub executes the frozen arguments, recording the confirming subject as the principal. The conversation
cannot alter what runs, and the model cannot confirm its own request.

### A scheduled routine

1. The cron trigger fires in the configured timezone. If the previous run is still going, this run is
   skipped, not queued.
2. Each step is called through the module API as `principal: scheduler`. The gate applies, with the
   scheduler's own rules: it usually needs no confirmation, and it is denied whole families of tools by
   glob (locks, deletions).
3. A failing step aborts the routine. Partial results and the index of the failing step go on the bus
   and into the response.
4. There is no model in this path at all.

### A state change

1. The supervisor changes a module's state, or drains a line from its stderr.
2. It publishes to `module.state_changed` or `module.log`. The publish returns immediately regardless of
   how many consumers there are or how slow they are.
3. A background task mirrors state changes into the database, so a post-mortem after a restart still
   shows what happened, and `vahub doctor` and the `vahub_module_state` metric read the current value.
4. None of this reaches the web. The assistant page subscribes to one topic only, and module state and
   logs are operator information that stays on the host.

### A confirmation

1. A destructive call is stored with its arguments frozen, and a `policy.confirmation_required` event is
   published.
2. The assistant page, subscribed to that one topic, shows a prompt naming the module, the tool and the
   arguments, each inserted as text and never as markup. Its subscription is drained by a single consumer,
   because concurrent sends on one WebSocket are not safe.
3. A person presses Confirm within `policy.confirm_ttl_s`. The hub executes the frozen arguments and
   records the confirming subject as the principal. The conversation cannot alter what runs, and the
   model cannot confirm its own request.

## Non-goals

These are decisions, not gaps waiting to be filled.

* **Not a home automation platform.** The hub does not talk to devices, speak Zigbee, or hold a device
  registry. It talks to whatever already owns your devices, through a module.
* **No in-process plugin API.** Plugins would be the fastest way to lose every property the process
  boundary provides.
* **No prompt-level security.** Prompts shape behaviour; they do not authorize anything. Anything that
  matters is enforced in code, at the gate.
* **No authentication or user management in the hub.** Doing it well means TLS, sessions, password
  storage and recovery flows, and doing it badly is worse than not doing it. That job belongs to a
  reverse proxy that already does it.
* **No distributed deployment.** One process, one node, SQLite. The workload is a household.
* **No autonomous operation.** The assistant acts when asked, or when a routine you wrote fires. It does
  not decide on its own that now is a good time to do something.
* **No model hosting.** The hub speaks to an OpenAI-compatible or Anthropic endpoint, local or remote,
  and does not manage weights, GPUs or inference servers.
* **No general purpose sandbox.** Modules get a process, a uid and a stripped environment. Namespaces,
  seccomp and cgroups are the host's job, and the deployment material shows how to apply them.
