# Security

vahub gives a language model the ability to act on your home and your accounts.
This document says plainly what is trusted, what is not, what the policy gate
does about it, and where the gate stops helping. It also says how to report a
vulnerability.

`docs/security.md` covers the same boundary from the operator's side, with the
configuration that goes with each decision. This file is the shorter statement
of the model itself.

## Reporting a vulnerability

Report privately through GitHub Security Advisories:

https://github.com/LynnDelpy/vahub/security/advisories/new

Please do not open a public issue for a vulnerability, and please do not post a
working exploit before a fix exists.

Useful things to include: the version or commit, the configuration that makes it
reachable (with secrets removed), what an attacker gains, and the smallest
sequence that demonstrates it. If a module is involved, say which one and where
it came from.

What to expect: an acknowledgement within three days, an assessment within two
weeks, and credit in the release notes unless you prefer otherwise. This is a
volunteer project with no bug bounty. Once a fix is released, the advisory is
published.

Supported versions:

| Version | Status |
| --- | --- |
| 0.1.x | supported |
| older | none exist yet |

## What the system is

Three parts, with a boundary between each:

1. An agent that talks to a language model and proposes tool calls.
2. A policy gate that authorizes or refuses every call, in code.
3. Modules, which are separate processes that speak MCP over stdin and stdout
   and hold the credentials for whatever they talk to.

The gate sits between the agent and the modules, and the scheduler and the
development endpoint go through it as well. There is no path from a proposed
action to a module that skips it.

## What is trusted, and what is not

**Not trusted: the model's output.** Every tool call it proposes is a request,
never a decision. The model is a component that can be steered by its input.

**Not trusted: anything a tool returns.** The title of a calendar event, the
body of a notification, the name of a device, the text of a transit alert. All
of it enters the model's context, and any of it can contain instructions aimed
at the model. This is prompt injection, and it is not solved by prompting. It is
contained by refusing the resulting calls at the gate, and by keeping the
console from rendering module output as markup.

**Not trusted: the module process.** A module runs on your machine, with the
configuration you gave it. The hub does not import it, shares no memory with it,
and can kill it, but a module is still code you chose to run.

**Trusted: vahub.yaml.** The configuration file is the operator's word. Anyone
who can edit it can grant anything. Protect it accordingly.

**Trusted: the operator at the console.** The hub has no user model. It assumes
that whoever reaches the web interface is allowed to use it, which is why the
default bind address is loopback.

## What the gate does

* **Default deny.** A tool with no rule in `policy.rules` is refused. Adding a
  module does not grant it anything.
* **Arguments, not just names.** Each rule lists the arguments it permits and
  the values they may take (`in`, `matches`, `range`, `max_len`). An argument
  the rule does not describe is refused rather than passed through. This is the
  part that matters: a Home Assistant long lived token is admin or nothing, so
  once `light_turn_on` is allowed at all, the only thing standing between the
  model and every device in the house is the constraint on `entity_id`.
* **Per principal.** The agent, the scheduler and a person confirming at the
  console are different principals. A principal has deny patterns and a list of
  classes it must have confirmed. The scheduler can act unattended and still be
  denied locks.
* **Classes and confirmation.** A tool is `read`, `write` or `destructive`. A
  class listed under a principal's `confirm` is not executed. The call becomes a
  pending confirmation with its arguments frozen, a time to live, and a decision
  made out of band. Later turns in the conversation cannot alter what the
  confirmation executes.
* **Catalog filtering.** Tools the principal could never call are not offered to
  the model, so it does not plan around them.
* **Audit.** Every call is recorded with its principal, arguments, decision and
  result, including refusals. Arguments named in a manifest's `audit.redact` are
  masked before they are written.
* **Manifests do not grant.** A manifest's `tools` block is what the module
  claims about itself. It is advisory. Only `vahub.yaml` authorizes.

## What the gate does not protect against

* **A rule you wrote too widely.** `matches: ".*"` is an allow rule with extra
  steps. The gate enforces what you wrote, and cannot tell an intended entity
  pattern from an accidental one.
* **What a module does on its own.** The gate sees calls the hub makes. It does
  not sit between the module and the service the module talks to. A module
  holding an admin token can use it whenever it likes, for anything, without a
  tool call. This is the reason installing a module is a trust decision and the
  reason a module gets only the environment variables its manifest declares.
* **A malicious module.** The hub reduces the damage (minimal environment, no
  shell, argv lists only, an optional dedicated uid per module, one call in
  flight at a time, untrusted results guarded rather than parsed hopefully), but
  a module is a process on your machine. Read what you install and pin it to a
  revision.
* **Exfiltration through permitted tools.** A permitted read tool combined with
  a permitted tool that sends arbitrary text is a general purpose channel out.
  If a notify module can send any string anywhere, injected instructions can use
  it to forward whatever the agent has read. Constrain destinations, or class
  the tool so it needs confirmation.
* **The model provider.** Prompts, transcripts and tool results go to whichever
  provider you configure, and speech providers receive your audio. The only
  configurations that keep this on your machine are a local model endpoint and
  browser side speech.
* **An exposed console.** The hub has no authentication of its own. Binding it
  to anything other than loopback without an authenticating reverse proxy in
  front makes every API route, including the confirmation routes, reachable by
  anyone who can connect. `web.auth_subject_header` records who the proxy says
  is acting; it is written to the audit log and never used as an authorization
  input, because a header is trivially forged by anyone who reaches the port
  directly.
* **`web.dev_tools_endpoint`.** It executes a tool without the agent. It is
  still gated, and it is still unauthenticated. It defaults to off and belongs
  on a development machine only.
* **Denial of service and cost.** The budgets bound one turn (iterations, tool
  result bytes, tokens, wall clock). They keep a loop from becoming an
  unbounded bill. They are not a defence against someone who can reach the API
  and keep asking.
* **The state directory.** Conversation history, the audit trail and pending
  confirmations live in SQLite under `hub.state_dir`. It is not encrypted.
  Anyone who can read the file can read your conversations.

## Deploying with the boundary intact

* Keep `web.host` on loopback, or put an authenticating reverse proxy in front
  and let only the proxy reach the port.
* Keep `policy.default: deny`. Write the narrowest constraints that let the
  thing you want actually work, and check them with the audit log rather than
  by assumption.
* Class anything you would not want to happen unattended as `destructive`, and
  give the scheduler a deny pattern for it.
* Give every module its own uid in the manifest, and run the hub where dropping
  privileges is possible.
* Keep secrets out of `vahub.yaml`. Use `${VAR}` or `${file:/path}` so the
  values come from systemd credentials, Docker secrets or Kubernetes secrets,
  and list secret bearing arguments under `audit.redact`.
* Restrict outbound network access to the endpoints your modules and your model
  provider actually need.
* Install modules from a pinned tag or commit. The registry refuses a moving
  branch as a source for this reason.

## Known limitations recorded on purpose

These are design decisions, not oversights, and issues asking about them are
welcome but will get this answer:

* Prompt injection is contained, not prevented. The gate assumes the model can
  be convinced to propose anything.
* The hub does not authenticate users. Authentication belongs to the proxy.
* Module code is not sandboxed beyond process separation, a minimal environment
  and an optional uid. Stronger isolation (namespaces, seccomp) is a deployment
  concern and is not built in.
