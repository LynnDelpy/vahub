# Deployment

Running vahub for real. This document covers the container image, a compose stack with a
TLS proxy, a systemd unit, how secrets get in, backups, upgrades, what to monitor, and a
hardening checklist.

One thing to understand before anything else: **the hub has no authentication of its own.**
Whoever can reach its port can drive it, read the audit log, and confirm a pending
destructive action. It binds to `127.0.0.1` by default for that reason. Exposing it to a
network is a deliberate act that requires a proxy in front, and the rest of this document
assumes you do it that way.

## Layout on disk

| path | contents | who writes it |
|---|---|---|
| `/etc/vahub/vahub.yaml` | the whole configuration: runtime, model, budgets, policy, schedules | you |
| `/etc/vahub/modules.d/*.yaml` | one manifest per installed module | `vahub module add` |
| `/var/lib/vahub/vahub.db` | SQLite: audit log, conversations, pending confirmations, token usage | the hub |
| `/var/lib/vahub/modules/<name>/` | per module working directory | the module |
| `/var/lib/vahub/` (venv per module) | one virtualenv per installed module | `vahub module add` |

`/etc/vahub` holds hand written configuration and the manifests, and only `vahub module add`
writes there, which you run by hand. `/var/lib/vahub` must be writable by the hub at all
times: it holds the database and every module's virtualenv. Both paths are set by
`hub.state_dir` and `hub.modules_dir` in [configuration.md](configuration.md).

## Choosing a deployment shape

| shape | isolation | when |
|---|---|---|
| systemd on the host or in an LXC | process, plus one uid per module | you want per module uid separation and host level firewalling. This is the recommended production shape. |
| Docker Compose | container | you want one command, a bundled TLS proxy, and easy teardown. Per module uid separation is not available inside a single container running as one user. |

Both run the same code. The difference is what a compromised module can reach.

## Container image

The repository ships [deploy/docker/Dockerfile](../deploy/docker/Dockerfile). Build it from
the repository root:

```bash
docker build -f deploy/docker/Dockerfile -t vahub:local .
```

It is a two stage build: the first stage builds a virtualenv with uv, the second copies only
that virtualenv into a clean base, so the final image has no compiler, no headers and no
build cache. Details that affect how you run it:

* **It runs as uid 10001, not root.** Modules therefore run as that same uid. Inside a
  single container, `runtime.user` in a manifest has no effect. If you want one uid per
  module, use the systemd shape.
* **The entrypoint is the CLI.** `docker run vahub:local doctor` and
  `docker run vahub:local module list` work without remembering a path. `CMD` is `run`.
* **`HOME` and the caches point at `/var/lib/vahub`**, which is why the container can run
  with a read only root filesystem. Everything writable lives on the state volume.
* **`VAHUB_CONFIG=/etc/vahub/vahub.yaml`** is set in the image, so mount your configuration
  there. Do not bake it into the image.
* **git and uv are present in the runtime image**, because `vahub module add` fetches a
  module from a pinned revision and builds a virtualenv for it at runtime. There is
  deliberately no compiler, so a module whose dependencies have no wheels will fail to
  install. That is a reason to prefer modules that publish wheels.
* **The healthcheck hits `/health` on port 8080.** If you move `web.port`, override the
  healthcheck in compose too: a `HEALTHCHECK` cannot read the parsed configuration.

## Docker Compose with a TLS proxy

[deploy/docker/docker-compose.yml](../deploy/docker/docker-compose.yml) is the production
stack: the hub, and a Caddy proxy that terminates TLS and demands a client certificate.

```bash
cd deploy/docker
cp ../../examples/vahub.yaml ./vahub.yaml      # then edit it
./gen-certs.sh                                 # throwaway CA, server and client certs
mkdir -p secrets && install -m 0600 /dev/null secrets/llm_api_key
$EDITOR secrets/llm_api_key
docker compose up -d
```

Then import `certs/client.p12` into the browser or device that should be allowed in, and
open `https://localhost:8443`. Without the client certificate the TLS handshake fails and
nothing reaches the hub.

The shape of the stack, and why:

* **The hub publishes no port at all.** It is reachable only over the internal compose
  network, from the proxy. `VAHUB_WEB__HOST=0.0.0.0` is set because inside a container
  loopback means "unreachable by the proxy"; the container network namespace is the
  boundary, and nothing publishes the port to the host.
* **The proxy binds to `127.0.0.1:8443` by default.** Set `VAHUB_BIND` to a LAN address once
  you have issued certificates to the devices that need one, and `VAHUB_DOMAIN` to the name
  in the server certificate.
* **The API key arrives as a Docker secret**, mounted at `/run/secrets/llm_api_key`. The
  configuration references it as `${file:/run/secrets/llm_api_key}`, so the key is not in
  the YAML, not in the compose file, and not in `docker inspect` output.
* **Module credentials go in `module.env`**, next to the compose file, because they must be
  environment variables of the hub process. See [Secrets](#secrets). The file is optional:
  a hub whose modules need no credentials does not need one.
* **`init: true`** so PID 1 reaps orphaned grandchildren. The hub reaps the module processes
  it spawned, but a module that forks and dies leaves its children to PID 1, and CPython
  does not reap strangers.
* **`stop_grace_period: 30s`**, matching the hub's own shutdown sequence. A shorter grace
  period kills modules mid call.
* **`read_only: true`, `cap_drop: ALL`, `no-new-privileges`, and cpu, memory and pid
  limits.** The pid limit matters: a module stuck in a fork loop must not take the host
  with it.
* **The certificates from `gen-certs.sh` are not a production PKI.** The CA key is written
  unencrypted next to the certificates it signs, on the machine serving the traffic. It
  exists so a first install takes one command. For a deployment you keep, move the CA key
  offline, issue one certificate per device, and either check a revocation list or keep
  lifetimes short enough that expiry is your revocation mechanism.

The web surface is the assistant and nothing else: asking something, speaking something, and
approving a destructive action a person was asked to confirm. There is no operator interface
over HTTP. Module state, module stderr and the audit log are read with the CLI on the host
(`vahub doctor`, `vahub audit`), which needs the same access that changing the policy needs.
[deploy/docker/Caddyfile](../deploy/docker/Caddyfile) refuses the old operator paths with a
404 as defence in depth, so a future regression cannot quietly put a debugger on the network.
Developers work through [docker-compose.dev.yml](../deploy/docker/docker-compose.dev.yml),
which publishes port 8080 on loopback, mounts the source over the installed package, and
turns on debug logging. Do not use that overlay on a machine you care about.

Three points about the proxy arrangement generally:

* **Client certificates, not a password.** The hub can perform destructive actions in your
  home. A bearer token in a browser is one phishing page away from someone else; a client
  certificate cannot be typed into a form. Issue one per device, keep the CA key offline,
  and keep a revocation path.
* **`X-Auth-Subject` is recorded, never trusted for authorization.** It appears in the
  audit log as the principal that confirmed an action. The hub does not use it to decide
  anything. That is why the proxy must replace, not append, the header: a client supplied
  value would otherwise pollute the audit trail.
* **`web.origin_allowlist` is checked explicitly**, on state changing requests and on the
  WebSocket. Same origin does not stop a cross origin POST and does not apply to WebSockets
  at all. Set it to the exact origin users type into the address bar. Do not set `"*"` on
  anything reachable from a browser that also visits the rest of the internet.

If you terminate TLS elsewhere (an existing nginx, a tunnel, a mesh), keep the two
properties: the hub's port is not reachable directly, and the proxy sets `X-Auth-Subject`
by replacement.

## systemd

The recommended production shape. Put the hub in its own unprivileged LXC or on a host that
does nothing else. The unit ships as [deploy/systemd/vahub.service](../deploy/systemd/vahub.service).

```bash
# a dedicated account, so uids stay stable across restarts
useradd --system --no-create-home --home-dir /var/lib/vahub \
        --shell /usr/sbin/nologin vahub

# the hub in its own virtualenv, where the unit expects it
python3.12 -m venv /opt/vahub/venv
/opt/vahub/venv/bin/pip install vahub

install -d -m 0750 -o root -g vahub /etc/vahub /etc/vahub/credentials
vahub init --config /etc/vahub/vahub.yaml
$EDITOR /etc/vahub/vahub.yaml

install -m 0644 deploy/systemd/vahub.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now vahub
journalctl -u vahub -f
```

The decisions in that unit that you should not undo without thinking:

* **`Type=exec`.** The hub runs in the foreground and does not fork. On `SIGTERM` it drains
  the web server, stops the scheduler, terminates modules (`SIGTERM`, then `SIGKILL`) and
  closes the database. `TimeoutStopSec=30` and `KillMode=mixed` give that sequence room. A
  shorter stop timeout kills modules mid call.
* **A dedicated user, not `DynamicUser=yes`.** The state directory outlives the process and
  the supervisor may need to drop to fixed module uids, both of which need uids that do not
  change between restarts.
* **`StartLimitBurst=5` in a minute.** A hub that cannot stay up for five attempts is not
  going to be fixed by a sixth. Leaving it failed makes the state visible instead of hiding
  it in a restart loop.
* **`RestrictAddressFamilies` includes `AF_NETLINK`.** glibc uses netlink to enumerate
  interfaces during name resolution, so removing it breaks every outbound HTTPS call with a
  confusing error.
* **`SystemCallFilter=@system-service`** keeps `@setuid`, which the supervisor needs to drop
  privileges for a module that declares `runtime.user`.
* **`MemoryDenyWriteExecute=yes`.** CPython does not need writable executable memory. A
  module that does (a libffi closure, a JIT) will fail to start. Drop the line if that is
  the module you want, and know what you gave up.
* **`OOMPolicy=continue`.** The kernel killing one module process is not a reason to stop
  the hub. The supervisor restarts modules by itself.
* **`MemoryMax=1G`, `TasksMax=512`.** A module that leaks should degrade the assistant, not
  the machine. Raise them if you run many modules.

### One uid per module

`runtime.user` in a manifest makes the supervisor drop to that uid before `exec`, clearing
the parent's supplementary groups first with `initgroups` so the module does not inherit
group memberships it should not have. It needs the privilege to change uid at all, which
the shipped unit deliberately removes, so turning it on means editing it back in:

```ini
CapabilityBoundingSet=CAP_SETUID CAP_SETGID
AmbientCapabilities=CAP_SETUID CAP_SETGID
```

The most reliable form is a hub that starts as root with exactly that bounding set, since
that is the case every implementation of privilege dropping handles. When the supervisor
cannot drop (no privilege, or an unknown user name), it logs a warning and starts the
module as itself rather than refusing to start it, so check the logs once after you enable
this instead of assuming it took effect.

Then create a user per module and give it only what it needs:

```bash
useradd --system --no-create-home --shell /usr/sbin/nologin vahub-mod-tasks
install -d -o vahub-mod-tasks -g vahub-mod-tasks -m 0700 /var/lib/vahub/modules/tasks
install -o vahub-mod-tasks -g vahub-mod-tasks -m 0400 \
        /etc/vahub/credentials/tasks_token /etc/vahub/credentials/tasks_token
```

```yaml
runtime:
  command: ["{venv}/bin/python", "-m", "vahub_mod_tasks"]
  user: vahub-mod-tasks
  cwd: "{state}/modules/tasks"
```

This is a real trade: the hub gains the ability to become those users in exchange for each
module running as nobody in particular. It is worth it when you run several modules whose
credentials are worth different amounts, and it is not worth much for a single module. If
you skip it, everything runs as `vahub` and a compromised module has whatever that user
has.

A credential file must be readable by the module's uid, not only by the hub's. Check the
ownership when you add a module: the failure mode is a module that starts cleanly and then
reports an authentication error from its backend.

## Secrets

Nothing secret belongs in `vahub.yaml`. Two references are supported in any string value:

```yaml
llm:
  api_key: ${VAHUB_LLM_API_KEY}                                  # from the environment
  # or
  api_key: ${file:/run/credentials/vahub.service/llm_api_key}    # systemd credential
  # or
  api_key: ${file:/run/secrets/llm_api_key}                      # docker secret
```

Prefer the file form. A file has permissions, does not appear in `/proc/<pid>/environ`, is
not inherited by children, and does not end up in a crash report. systemd credentials,
Docker secrets and Kubernetes secrets all present as files.

A missing reference is a startup error, not an empty string. A hub that starts with a
silently empty API key and fails on the first request is worse than one that refuses to
start.

### Module credentials are different

Modules do not read `vahub.yaml`. Each module declares the environment variable names it
needs, and the supervisor passes exactly those, taken from the hub's own environment, plus
`PATH`, `HOME` and `LANG`. There is no blanket passthrough, so a token meant for one module
is not visible to another.

That means a module credential has to be in the hub's environment. Two ways:

1. **The value in the environment.** The shipped unit reads `/etc/vahub/module.env`, and
   the compose stack reads `module.env` next to the compose file:

   ```
   TASKS_URL=https://tasks.example.org
   TASKS_TOKEN=...
   ```

   Keep that file `0600` and owned by root. This is the simple option, and it is the reason
   `audit.redact` exists: the value lives in the hub's environment for the lifetime of the
   process.

2. **A path in the environment, the value in a file.** Well written modules accept both:

   ```
   TASKS_URL=https://tasks.example.org
   TASKS_TOKEN_FILE=/run/credentials/vahub.service/tasks_token
   ```

   The secret itself never becomes an environment variable. Prefer this whenever the module
   supports it, and pair it with a `LoadCredential=` line or a Docker secret. Remember that
   the file has to be readable by the uid the module runs as.

### Rotating

Rotate at the source, update the file or the environment file, restart the hub. There is no
reload: a running module already read its credential. Restarting the hub restarts modules.

Check `audit.redact` in each module's manifest lists any tool argument that could carry a
secret, and grep your audit database once after installing a new module. The audit log is
long lived and is exactly the kind of file that gets attached to a bug report.

## Backups

Two things are worth backing up, for different reasons.

**`/etc/vahub`** is your configuration and your policy. It is small, it is hand written, and
losing it means rebuilding your rules from memory. Keep it in version control, with the
secrets held out (`vahub.yaml` should reference files, so this is easy). This is the backup
that actually matters.

**`/var/lib/vahub/vahub.db`** is the audit log, conversation history, pending confirmations
and token usage. Losing it costs history, not function: the hub recreates an empty database
on start.

Back up the database with SQLite's own mechanism, never with `cp`. The hub keeps the
database open, and a plain copy of a live SQLite file with a write ahead log beside it is a
corrupt database that restores without complaining.

```bash
install -d -m 0700 /var/backups/vahub
sqlite3 /var/lib/vahub/vahub.db \
  "VACUUM INTO '/var/backups/vahub/vahub-$(date +%F).db'"
```

`VACUUM INTO` takes a read transaction and writes a consistent, defragmented copy while the
hub keeps working. No downtime, one file, no partial write.

A daily version of exactly that ships as
[deploy/systemd/vahub-backup.service](../deploy/systemd/vahub-backup.service) and its timer,
writing to `/var/backups/vahub` and keeping fourteen days:

```bash
install -d -m 0700 -o vahub -g vahub /var/backups/vahub
install -m 0644 deploy/systemd/vahub-backup.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now vahub-backup.timer
systemctl start vahub-backup.service     # run it once now, and check the output
```

The backup unit deliberately does not depend on `vahub.service`. A backup of a stopped hub
is still a backup, and a failing hub is exactly when yesterday's database matters.

The backup contains conversation transcripts and every tool call ever made in your home.
Treat it with the same care as the secrets, and encrypt it if it leaves the machine.

### Restore

```bash
systemctl stop vahub
cp /var/backups/vahub/vahub-2026-08-01.db /var/lib/vahub/vahub.db
chown vahub:vahub /var/lib/vahub/vahub.db
rm -f /var/lib/vahub/vahub.db-wal /var/lib/vahub/vahub.db-shm
systemctl start vahub
vahub doctor
```

Removing the stale `-wal` and `-shm` files matters: leaving them beside a restored database
is one of the few ways to produce genuine corruption.

To rebuild from nothing: reinstall the package, restore `/etc/vahub`, run
`vahub module add` for each manifest you have (the manifests name their exact pinned
sources), and start. Test this once, on a spare machine, before you need it.

## Upgrades

```bash
# 1. read the release notes, then back up config and database
sqlite3 /var/lib/vahub/vahub.db "VACUUM INTO '/var/backups/vahub/pre-upgrade.db'"

# 2. upgrade the hub
/opt/vahub/venv/bin/pip install --upgrade vahub

# 3. check before restarting: config loading is strict, and a renamed or removed
#    key is an error rather than a silently ignored line
vahub doctor

# 4. restart
systemctl restart vahub
journalctl -u vahub -n 50
```

For the container shape, pull or rebuild the image and `docker compose up -d`. The state
volume carries over; the database migrates on start.

Module upgrades are separate and independent:

```bash
vahub module upgrade --dry-run
vahub module upgrade --yes
vahub doctor            # new tools arrive with no policy rule, and are denied
systemctl restart vahub
```

Rollback is: install the previous version, restore the pre upgrade database if the schema
changed, restart. Which is why step 1 exists.

## Observability

### Logs

Structured JSON on stdout, one object per line, captured by journald or the container
runtime. Useful events: `hub_starting`, `hub_ready`, `module_discovered`,
`module_state_changed`, `module_ready`, `module_exited`, `module_restart_backoff`,
`module_failed_permanently`, `module_handshake_failed`.

```bash
journalctl -u vahub -f | jq -c 'select(.event | startswith("module_"))'
```

Module stderr is captured separately, kept as the last 200 lines per module, and published on
the event bus. It stays on the host: it is in the service log, not on the web. That is where a
module's own error messages go.

### Metrics

Prometheus format at `GET /metrics`, on the same port as everything else. It is behind the
same proxy, so scrape it through the proxy with a client certificate, or scrape it from
localhost.

The core series:

| metric | type | labels | use |
|---|---|---|---|
| `vahub_module_state` | gauge | `module`, `state` | 1 for the current state, 0 otherwise. The single most useful signal. |
| `vahub_tool_calls_total` | counter | `module`, `tool`, `result` | `result` is `ok`, `timeout`, `error`, `tool_error` or `bad_result`. |
| `vahub_tool_latency_seconds` | histogram | `module`, `tool` | Where time goes. |
| `vahub_bus_dropped_total` | counter | `topic` | Event bus backpressure: a subscriber too slow to keep up. |

Curl `/metrics` on your own build for the full list, which grows.

### What to alert on

Alert on things that mean the assistant is quietly not working. Most vahub failures are
quiet: nothing crashes, the hub keeps answering, and one module has stopped doing anything.

```promql
# A module has given up entirely. Page-worthy: it will not come back on its own.
max_over_time(vahub_module_state{state="failed"}[5m]) == 1

# A module has been degraded for 15 minutes. Usually an expired token or a
# backend that is down, and the assistant is answering "not available" meanwhile.
min_over_time(vahub_module_state{state="degraded"}[15m]) == 1

# Tool calls are failing. A backend that changed its API looks exactly like this.
sum by (module) (rate(vahub_tool_calls_total{result!="ok"}[10m]))
  / sum by (module) (rate(vahub_tool_calls_total[10m])) > 0.2

# Timeouts specifically: a module is blocking, and because calls are serialised
# per module, one slow tool stalls every other tool in that module.
sum by (module, tool) (rate(vahub_tool_calls_total{result="timeout"}[10m])) > 0

# A module returning results the hub cannot parse. Almost always a module bug.
sum by (module) (rate(vahub_tool_calls_total{result="bad_result"}[15m])) > 0

# Event bus dropping messages: a consumer, usually a WebSocket client, is behind.
rate(vahub_bus_dropped_total[10m]) > 0

# The hub is not up at all.
up{job="vahub"} == 0
```

Do not alert on a single restart. `restart.reset_after_s` exists precisely because isolated
blips are normal; a module that restarts and stays up is fine. Alert on `failed`, which is
the state a module reaches after it has spent its retry budget.

Also worth watching, from the audit log rather than from metrics: policy denials. Every
call, allowed or denied, is recorded with its principal, its arguments and the gate's
decision. A rising denial rate for one tool usually means the model has learned a request
it cannot fulfil, which is either a policy that is too tight or a description that is
misleading.

```bash
vahub audit --json | jq -r '.[] | [.module, .tool, .decision] | @tsv' \
  | sort | uniq -c | sort -rn | head
```

### Health endpoint

`GET /health` is unauthenticated and cheap, for the container healthcheck and the load
balancer. It reports that the process is serving. It is not a statement about modules; use
`vahub doctor` on the host or the metrics for that.

## Hardening checklist

Configuration:

* [ ] `web.host` is `127.0.0.1`, or the port is otherwise unreachable except through the
      proxy.
* [ ] `web.origin_allowlist` names the exact origins you use, and is not `"*"`.
* [ ] `policy.default` is `deny`.
* [ ] Every rule constrains every argument its tool accepts. An argument with no constraint
      entry is refused, so a rule with an empty `constraints` block is a tool that cannot
      be called with arguments, not a tool that accepts anything.
* [ ] Destructive tools are classed `destructive` and the `agent` principal has
      `confirm: [destructive]`.
* [ ] The `scheduler` principal denies anything that should never run unattended
      (`"*lock*"`, `"*unlock*"`, `"*delete*"`).
* [ ] `budgets.tokens_per_day` is set. It bounds both cost and a runaway loop.
* [ ] `vahub.yaml` contains no secret values, only `${...}` references.

Network:

* [ ] TLS terminates at a proxy that requires a client certificate, or the hub is reachable
      only from a network you control end to end.
* [ ] The proxy sets `X-Auth-Subject` by replacement, not by appending.
* [ ] Egress is restricted to what the hub and its modules actually need: the model
      provider, and each module's backend. An assistant with an outbound path to anywhere
      is a data exfiltration path with a language model attached.
* [ ] `/metrics` and the assistant page are not published to the internet unauthenticated.

Host:

* [ ] The hub runs as a dedicated user, or as root only to drop to one uid per module.
* [ ] `/etc/vahub` and every secret file are `0400` or `0600`, owned appropriately.
* [ ] The systemd hardening directives above are in place, and `systemd-analyze security
      vahub.service` returns a score you are willing to defend.
* [ ] The state directory is on a filesystem that survives a reboot, and it is backed up.
* [ ] Unattended upgrades cover the host, and you check hub releases deliberately.

Operations:

* [ ] `vahub doctor` runs clean, or every remaining warning is one you decided to keep.
* [ ] Backups exist and a restore has been tested at least once.
* [ ] Alerts exist for `failed` modules and for `up == 0`, and they reach a person.
* [ ] Someone other than you can find the audit log and answer "what did it do at 03:00".
