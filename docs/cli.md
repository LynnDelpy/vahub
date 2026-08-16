# Command reference

`vahub` is the only executable the package installs. It creates a configuration, runs the
hub, checks a deployment, and manages modules.

```
vahub [GLOBAL OPTIONS] COMMAND [ARGS]

  init                  write a starter vahub.yaml
  run                   run the hub in the foreground
  doctor                check the configuration, the modules and the environment
  config                show the effective configuration
  module list           list installed modules
  module search         search the registry
  module info           show what is known about a module
  module add            install a module
  module remove         uninstall a module
  module upgrade        install a newer version of a module
  module verify         check a module against the module contract
```

## Global options

| option | meaning |
|---|---|
| `-c`, `--config PATH` | Path to `vahub.yaml`. Overrides the search order below. |
| `--version` | Print the version and exit. |
| `--help` | Print help for the command or subcommand and exit. |

Every command that reads configuration follows the same search order:

1. `--config PATH`
2. `$VAHUB_CONFIG`
3. `./vahub.yaml`
4. `/etc/vahub/vahub.yaml`

Every setting can also be overridden by an environment variable using the `VAHUB_` prefix,
with `__` separating levels: `VAHUB_WEB__PORT=9000` sets `web.port`, and
`VAHUB_LLM__MODEL=...` sets `llm.model`. Only scalar values can be set this way. Policy
rules and schedules stay in the file. See [configuration.md](configuration.md) for what can
be set.

## Exit codes

| code | meaning |
|---|---|
| `0` | Success. For `doctor`, no errors were found. |
| `1` | The command failed: a configuration error, a failed check, a module that would not start. |
| `2` | Usage error: an unknown command, a missing argument, a bad flag. |

Anything that fails prints the reason on stderr, in a form meant to be read by a person
fixing it under time pressure.

## vahub init

```
vahub init [--config PATH] [--force]
```

Writes a starter `vahub.yaml` with the defaults, a commented policy section, and no
secrets. It refuses to overwrite an existing file unless `--force` is given.

| option | meaning |
|---|---|
| `--config PATH` | Where to write. Defaults to `./vahub.yaml`. |
| `--force` | Overwrite an existing file. |

The generated file binds the web server to `127.0.0.1`, sets `policy.default: deny` and
uses `llm.provider: mock`, so a fresh install starts, does nothing dangerous, and needs no
credentials. Read it before you run anything else: it is the shortest description of what
the hub can be told to do.

## vahub run

```
vahub run [--config PATH] [--host HOST] [--port PORT] [--log-level LEVEL]
```

Runs the hub in the foreground: loads and validates the configuration, opens the database,
discovers and starts every configured module, starts the scheduler, and serves the web API
and console. This is what a service manager should execute.

| option | meaning |
|---|---|
| `--host HOST` | Override `web.host`. |
| `--port PORT` | Override `web.port`. |
| `--log-level LEVEL` | Override `hub.log_level`: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

Behaviour worth knowing:

* It does not fork or daemonise. Logs go to stdout, structured as JSON by default
  (`hub.log_format: console` gives human readable output for a terminal).
* `SIGTERM` and `SIGINT` start a graceful shutdown: stop accepting work, drain the web
  server, stop the scheduler, stop modules (`SIGTERM`, then `SIGKILL` after five seconds),
  close the database, exit.
* A module that fails to start does not stop the hub. It is reported and retried with
  backoff, and its tools are absent from the catalog until it is ready.
* A configuration error does stop the hub, before anything is spawned, with the offending
  key named.

## vahub doctor

```
vahub doctor [--config PATH] [--json]
```

Checks a deployment and reports what is wrong with it. Run it after `init`, after every
upgrade, and first when something is not behaving.

It reports on:

* the configuration file: whether it parses, validates, and which path was used,
* unresolved `${VAR}` and `${file:...}` references,
* the state directory and the database: present, writable, and the schema version,
* every manifest in `hub.modules_dir`: parses, validates, and the command exists,
* modules whose required configuration keys are missing, naming the keys,
* modules with no policy rule at all, which is the usual reason a module appears to do
  nothing,
* policy rules that name a module or tool that is not installed,
* tools whose arguments have no constraint entry, which the gate will refuse,
* schedules that reference a module or tool that is not installed, and invalid cron
  expressions,
* the language model configuration: provider, base URL, whether a key is present. It does
  not spend a request to check the key,
* whether the web server is bound to a non loopback address without a proxy in front.

Findings are printed as errors or warnings. The exit code is `1` if there is at least one
error, `0` if there are only warnings.

| option | meaning |
|---|---|
| `--json` | Machine readable output, for CI or a monitoring check. |

## vahub config

```
vahub config [--config PATH] [--json] [--path] [--reveal]
```

Prints the effective configuration: the file, plus `${...}` interpolation, plus `VAHUB_*`
environment overrides, after validation. This is what the hub will actually use, which is
often not what the file appears to say.

| option | meaning |
|---|---|
| `--json` | Print JSON instead of YAML. |
| `--path` | Print only the path of the configuration file that would be loaded, then exit. |
| `--reveal` | Print secret values in clear. Off by default. |

Secrets (`llm.api_key`, `speech.stt.api_key`, `speech.tts.api_key`) are replaced with
`***` unless `--reveal` is given, so the output is safe to paste into a bug report. Check
that before you paste anyway.

## vahub module list

```
vahub module list [--config PATH] [--json]
```

Lists installed modules, from the manifests in `hub.modules_dir`. For each module: name,
version, description, the declared tools with their classes, and its configuration status
(ready, or which required keys are missing).

This reads manifests, so it works whether or not the hub is running. It reports what is
installed and configured, not live process state. Live state (`ready`, `degraded`,
`failed`) comes from `vahub doctor`.

| option | meaning |
|---|---|
| `--json` | Machine readable output. |

## vahub module search

```
vahub module search [QUERY] [--registry URL] [--json]
```

Searches the registry index by name, description and tags. With no query, lists everything
in the index.

| option | meaning |
|---|---|
| `--registry URL` | Use another index. A URL or a local file path. |
| `--json` | Machine readable output. |

The registry is an index, not a store. A result tells you where a module's code lives, and
installing it runs that code on your machine. Read the module's homepage before you install
it.

## vahub module info

```
vahub module info NAME [--registry URL] [--json]
```

Shows what is known about a module: description, homepage, author, tags, the available
versions and which is `latest`, and for the resolved version its source, the configuration
keys it requires and accepts, the minimum hub version, and any notes.

If the module is also installed, the installed version and the resolved manifest are shown
alongside, so you can see whether an upgrade is available and what would change.

| option | meaning |
|---|---|
| `--registry URL` | Use another index. |
| `--json` | Machine readable output. |

## vahub module add

```
vahub module add NAME[==VERSION] [options]
vahub module add --source SPEC [options]
```

Installs a module: resolves the source, fetches it at its pinned revision, creates a
virtualenv for it, installs it there, writes the manifest into `hub.modules_dir`, and
reports which configuration keys still need values.

| option | meaning |
|---|---|
| `--source SPEC` | Install from a source directly, without the registry. |
| `--registry URL` | Use another index. |
| `--name NAME` | Override the installed name. Useful for running two instances of the same module against different backends. |
| `--set KEY=VALUE` | Record a configuration value for this module. Repeatable. |
| `--yes` | Do not prompt. Required in a script. |
| `--force` | Reinstall over an existing installation of the same name. |

`--source` accepts three forms:

```
git+https://host/repo.git@v1.2.3                       a git tag or commit sha
git+https://host/repo.git@v1.2.3#subdir=modules/foo    a module inside a larger repository
pypi:vahub-mod-foo==1.0.0                              a published wheel, pinned
./path/to/module                                       a local directory
```

A git source must be pinned to a tag or a full commit sha. A branch name is refused: an
install has to be reproducible, and "whatever `main` is today" is not.

Installing does not grant permission. The module's tools are denied until you add policy
rules for them in `vahub.yaml`, and `vahub doctor` will list them as installed with no
rule. This is deliberate: a module cannot grant itself permission by describing itself
generously.

Restart the hub afterwards, or it will pick the module up on its next start.

## vahub module remove

```
vahub module remove NAME [--yes] [--purge]
```

Removes the manifest and the module's virtualenv.

| option | meaning |
|---|---|
| `--yes` | Do not prompt. |
| `--purge` | Also delete the module's state directory under `hub.state_dir`. |

The module's policy rules are left in `vahub.yaml`. Removing configuration a person wrote
by hand is not the installer's decision to make. `vahub doctor` will flag them as rules for
a module that is not installed. Its rows in the audit log are never touched.

## vahub module upgrade

```
vahub module upgrade [NAME] [--to VERSION] [--registry URL] [--yes] [--dry-run]
```

Resolves the newest published version, installs it, and rewrites the manifest. With no
name, checks every installed module that came from the registry.

| option | meaning |
|---|---|
| `--to VERSION` | Install a specific version, including an older one. |
| `--registry URL` | Use another index. |
| `--yes` | Do not prompt. |
| `--dry-run` | Report what would change and exit. |

An upgrade can add tools, and new tools arrive denied, because the policy is yours and the
gate defaults to deny. It can also change the arguments an existing tool accepts, which can
silently break a constraint that named the old argument. Run `vahub doctor` after every
module upgrade, and read the module's release notes for argument changes.

Modules installed from a local path are skipped: there is nothing to resolve.

## vahub module verify

```
vahub module verify PATH_OR_NAME [--timeout SECONDS] [--json]
```

Runs a module against the module contract, without the agent and without a language model.
Give it a source directory or the name of an installed module.

```bash
vahub module verify ./my-module
vahub module verify tasks
```

It checks that:

* the manifest parses and validates,
* `runtime.command` resolves to something executable,
* the process starts, completes the MCP handshake and answers `tools/list` inside
  `restart.startup_timeout_s`,
* the tools exposed match the tools declared, in both directions,
* every tool has a description and an object typed input schema,
* no tool name uses the reserved `__` prefix except `__health`,
* `__health` exists, answers inside `health.timeout_s`, and returns an object,
* nothing but JSON-RPC was written to stdout,
* the process starts with only its declared configuration keys present.

It exits `1` on the first failure and prints what failed. Suitable for CI.

| option | meaning |
|---|---|
| `--timeout SECONDS` | Override the manifest's startup timeout for this run. |
| `--json` | Machine readable output. |

See [writing-modules.md](writing-modules.md) for what each check is protecting against.

## Common sequences

Fresh install:

```bash
vahub init --config /etc/vahub/vahub.yaml
$EDITOR /etc/vahub/vahub.yaml
vahub doctor
vahub run
```

Add a module and let it do something:

```bash
vahub module search calendar
vahub module info calendar
vahub module add calendar
$EDITOR /etc/vahub/vahub.yaml     # add policy.rules for its tools
vahub doctor
systemctl restart vahub
```

Develop a module locally:

```bash
vahub module verify ./my-module
vahub module add --source ./my-module --force
systemctl restart vahub
```

Upgrade everything:

```bash
vahub module upgrade --dry-run
vahub module upgrade --yes
vahub doctor
systemctl restart vahub
```
