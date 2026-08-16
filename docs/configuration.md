# Configuration

One file configures the hub: `vahub.yaml`. It holds the runtime settings, the language model, the
budgets, the policy that authorizes every action, and the scheduled routines. Module manifests live
outside it, one file per module, written by `vahub module add`.

`vahub init` writes a starting file for you. `vahub doctor` validates it, along with every installed
manifest, without starting anything.

## Where the file is read from

In order:

1. `$VAHUB_CONFIG`, if set.
2. `./vahub.yaml`, if it exists.
3. `/etc/vahub/vahub.yaml`.

If none of these exist the hub runs on built-in defaults, which is a working configuration that can hold
a conversation and do nothing else (the policy denies everything and no modules are installed).

## Precedence

Lowest to highest:

```
built-in defaults  <  vahub.yaml  <  VAHUB_* environment variables
```

Environment overrides exist for container deployments, where injecting one variable is easier than
templating a file. They apply to scalar values only. That is deliberate: nobody should be expressing a
policy rule as an environment variable.

## Strictness

Loading is strict. An unknown key is an error, not a silently ignored typo, and the hub refuses to
start. A misspelled `origin_allowlist` that quietly became an empty list would be a fail-open default,
and a policy section with a typo in it would be a policy you do not have.

Errors name the offending path and the file:

```
/etc/vahub/vahub.yaml is not valid:
  web.port: Input should be less than or equal to 65535
  policy.rules.homeassistant.light_turn_on.constraints.entity_id.matches: invalid regex
```

Regexes in policy constraints are compiled at load time for this reason. A broken pattern stops the hub
at startup rather than at the moment the rule is first exercised.

## Secrets

Secrets are never written into the file. Any string value may reference the environment or a file, and
the reference is expanded before validation:

```yaml
llm:
  api_key: ${VAHUB_LLM_API_KEY}            # from the environment
  # api_key: ${file:/run/secrets/llm_key}  # from a file, contents stripped
  # api_key: ${VAHUB_LLM_API_KEY:-}        # with a default when unset
```

* `${NAME}` reads an environment variable. If it is unset and no default is given, loading fails with a
  message naming the variable. Silently substituting an empty API key produces a confusing failure much
  later.
* `${NAME:-fallback}` uses `fallback` when `NAME` is unset.
* `${file:/path}` reads the file and strips surrounding whitespace. This is the form to use with systemd
  credentials, Docker secrets and Kubernetes secrets, all of which present a secret as a file. An
  unreadable path is an error.

References work anywhere a string appears, not only in `api_key`, and several can appear in one value.

## Environment overrides

An override is `VAHUB_` plus the path to a scalar, with `__` separating levels:

```bash
VAHUB_WEB__PORT=9000              # web.port
VAHUB_HUB__LOG_LEVEL=DEBUG        # hub.log_level
VAHUB_SPEECH__STT__PROVIDER=none  # speech.stt.provider
VAHUB_LLM__MODEL=gpt-4o-mini      # llm.model
```

Only variables whose first segment names an actual top-level section (`hub`, `web`, `llm`, `budgets`,
`speech`, `policy`, `schedules`) are treated as overrides. That distinction matters, because secrets are
referenced from the config and therefore exist in the environment too. `VAHUB_LLM_API_KEY` (one
underscore) is a secret you reference with `${VAHUB_LLM_API_KEY}`; `VAHUB_LLM__API_KEY` (two) is an
override that sets `llm.api_key` directly. Both work. Only the second is parsed as a config path.

Values are coerced: `true` and `false` become booleans, `null`, `none` and `~` become null, integers and
floats are parsed as numbers, a value starting with `[` or `{` is parsed as JSON, and anything else
stays a string. `VAHUB_CONFIG` is never treated as an override.

## Sections

### hub

```yaml
hub:
  log_level: INFO          # DEBUG | INFO | WARNING | ERROR
  log_format: json         # json | console
  timezone: Europe/Zurich  # used by the scheduler and by daily budgets
  state_dir: /var/lib/vahub
  modules_dir: /etc/vahub/modules.d
```

| Key | Default | Notes |
|---|---|---|
| `log_level` | `INFO` | |
| `log_format` | `json` | `console` is readable output for a terminal |
| `timezone` | `UTC` | An IANA name. Cron routines fire in this zone, and the daily token budget resets at its midnight |
| `state_dir` | `/var/lib/vahub` | The database, module virtualenvs and other runtime state. The database is `<state_dir>/vahub.db` and is not separately configurable |
| `modules_dir` | `/etc/vahub/modules.d` | Where `vahub module add` writes manifests, and where the supervisor discovers them |

For a non-root install, point both directories somewhere you own:

```yaml
hub:
  state_dir: ~/.local/state/vahub
  modules_dir: ~/.config/vahub/modules.d
```

### web

```yaml
web:
  host: 127.0.0.1
  port: 8080
  origin_allowlist: ["http://localhost:8080"]
  auth_subject_header: X-Auth-Subject
```

| Key | Default | Notes |
|---|---|---|
| `host` | `127.0.0.1` | Loopback by default. The hub has no authentication of its own, so binding it to a network address should be a deliberate act, not an accident of the default config |
| `port` | `8080` | |
| `origin_allowlist` | `["http://localhost:8080"]` | Browser origins allowed to call state-changing routes and open the event WebSocket. Same-origin does not stop a cross-origin POST and does not apply to WebSockets at all, so both are checked explicitly. `"*"` disables the check |
| `auth_subject_header` | `X-Auth-Subject` | The header your authenticating proxy sets. Recorded in the audit log as the acting principal. Informational only: it is never an authorization input |

### llm

```yaml
llm:
  provider: openai_compat        # openai_compat | anthropic | mock
  base_url: https://openrouter.ai/api/v1
  api_key: ${VAHUB_LLM_API_KEY}
  model: anthropic/claude-3.5-haiku
  temperature: 0.2
  max_tokens: 1024
  request_timeout_s: 60
  system_prompt: null
```

| Key | Default | Notes |
|---|---|---|
| `provider` | `mock` | `mock` is a keyword stub that needs no credentials. It is not language understanding; it exists so the whole loop can be exercised in tests |
| `base_url` | `https://openrouter.ai/api/v1` | Any OpenAI-compatible endpoint works: OpenAI, OpenRouter, Groq, Ollama, llama.cpp |
| `api_key` | none | Use a reference, not a literal |
| `model` | `mock` | |
| `temperature` | `0.2` | Range 0 to 2. Low, because this model is choosing actions, not writing prose |
| `max_tokens` | `1024` | Per response |
| `request_timeout_s` | `60` | |
| `system_prompt` | none | Replaces the built-in prompt. The built-in one tells the model that tool results are data and never instructions; if you replace it, keep that sentence |

### budgets

```yaml
budgets:
  iterations_per_turn: 8
  tool_result_bytes: 16384
  tokens_per_turn: 20000
  tokens_per_day: null
  wall_clock_text_s: 30
  wall_clock_voice_s: 8
```

| Key | Default | Notes |
|---|---|---|
| `iterations_per_turn` | `8` | Model calls per turn, 1 to 100 |
| `tool_result_bytes` | `16384` | Each tool result is truncated to this before re-entering the context. An iteration cap alone does not bound cost: one unfiltered result can dwarf a whole conversation |
| `tokens_per_turn` | `20000` | 0 disables the check |
| `tokens_per_day` | none | When set and reached, the agent stops answering until midnight in `hub.timezone`. Scheduled routines keep running, because they use no tokens |
| `wall_clock_text_s` | `30` | Deadline for a typed turn |
| `wall_clock_voice_s` | `8` | Deadline for a spoken turn. Shorter, because a spoken reply that arrives late is worse than one that admits it gave up |

Token accounting is best effort and comes from the provider's reported usage.

### speech

```yaml
speech:
  stt:
    provider: browser        # browser | openai_compat | none
    base_url: https://api.openai.com/v1
    api_key: ${VAHUB_STT_API_KEY}
    model: whisper-1
    request_timeout_s: 60
  tts:
    provider: browser        # browser | openai_compat | none
    base_url: https://api.openai.com/v1
    api_key: ${VAHUB_TTS_API_KEY}
    model: tts-1
    voice: alloy
    request_timeout_s: 60
```

`browser` means the client does the work locally: the browser transcribes speech and speaks the reply,
no credentials are needed and no audio leaves the machine. `openai_compat` posts audio to a
Whisper-style endpoint and requests speech from a `/audio/speech` endpoint. `none` disables that
direction entirely.

### policy

The gate. Covered in depth in [security.md](security.md); this is the syntax.

```yaml
policy:
  default: deny
  confirm_ttl_s: 60

  principals:
    agent:     { confirm: [destructive], deny: [] }
    scheduler: { confirm: [], deny: ["*lock*", "*unlock*", "*delete*"] }
    user:      { confirm: [], deny: [] }

  rules:
    time.get_current_time:
      class: read
      constraints:
        tz: { max_len: 64 }

    homeassistant.light_turn_on:
      class: write
      constraints:
        entity_id:      { matches: "^light\\.(kitchen|bedroom|hall)$", max_len: 64 }
        brightness_pct: { range: [1, 100] }

    homeassistant.lock_unlock:
      class: destructive
      constraints:
        entity_id: { in: ["lock.front_door", "lock.garage"] }
```

| Key | Default | Notes |
|---|---|---|
| `default` | `deny` | `allow` with no rules is rejected at load: it would hand the model unrestricted control |
| `confirm_ttl_s` | `60` | How long a pending destructive confirmation stays valid |
| `principals` | empty | Who is acting. Known principals are `agent`, `scheduler`, and the subject that confirms a pending call. An unlisted principal gets the empty default: no confirmations required, nothing denied by name, and still subject to `default: deny` |
| `rules` | empty | Keyed by `module.tool` |

A principal has two lists. `confirm` names tool classes that require out-of-band confirmation before
they run. `deny` holds glob patterns matched against `module.tool`, which is how you remove a whole
family of tools from one actor without touching the rules.

A rule has a `class` (`read`, `write`, or `destructive`) and a `constraints` map, one entry per allowed
argument. Constraint types:

| Constraint | Meaning |
|---|---|
| `in: [...]` | The value must be one of these |
| `matches: "regex"` | The value must be a string, and the pattern must match the WHOLE value (re.fullmatch). `light\.[a-z_]+` allows `light.kitchen` but not `light.kitchen extra` or `xlight.kitchen`; you do not need `^`/`$` |
| `range: [min, max]` | Numeric, inclusive |
| `max_len: n` | Length of the value as text |

Several constraints on one argument all apply. An argument that appears in a call but has **no**
constraint entry is denied. That is the property that keeps a rule from aging into a hole as a module
grows new parameters, and it means read-only tools with arguments still need to list them.

A tool with no rule at all is denied under `default: deny`, and is also hidden from the model's catalog,
so it does not plan calls that would only die at the gate.

### schedules

```yaml
schedules:
  - id: morning
    cron: "30 6 * * 1-5"      # 06:30 on weekdays, in hub.timezone
    enabled: true
    steps:
      - module: homeassistant
        tool: light_turn_on
        args: { entity_id: light.bedroom, brightness_pct: 40 }
        timeout_s: 10
      - module: time
        tool: speak_current_time
```

| Key | Default | Notes |
|---|---|---|
| `id` | required | Must be unique. A duplicate is a load error |
| `cron` | required | Five field crontab syntax, evaluated in `hub.timezone` |
| `enabled` | `true` | `false` keeps the definition without registering the job |
| `steps` | empty | Executed in order. A failing step aborts the rest of the routine |
| `steps[].timeout_s` | `10` | Per step |

Routines run as `principal: scheduler` and are subject to the gate like everything else. They involve no
model, so their cost is zero and their behaviour is fixed.

## Module configuration

Modules are not configured in `vahub.yaml`. Each installed module has its own manifest in
`hub.modules_dir`, written by `vahub module add`, which declares its command, its health and restart
parameters, the audit fields to redact, and the tools it offers. See
[writing-modules.md](writing-modules.md) for the full manifest.

Module credentials are environment variables, and a module only ever receives the keys its manifest
names in `config.required` and `config.optional`. There is no blanket pass-through of the hub's
environment, so a token belonging to one module is not visible to another. `vahub module add` reports
which keys a module needs and can record values for them at install time (`--set KEY=VALUE`, see
[cli.md](cli.md)). Otherwise supply them to the hub process itself, for example with an
`EnvironmentFile` in the systemd unit:

```ini
[Service]
EnvironmentFile=/etc/vahub/secrets.env   # 0600, owned by root
```

A module whose required keys are missing is discovered and shown by `vahub doctor` as `unconfigured`
instead of failing to spawn, so the reason is obvious.

## A complete example

```yaml
hub:
  log_level: INFO
  log_format: json
  timezone: Europe/Zurich
  state_dir: /var/lib/vahub
  modules_dir: /etc/vahub/modules.d

web:
  host: 127.0.0.1
  port: 8080
  origin_allowlist: ["https://hub.example.internal"]

llm:
  provider: openai_compat
  base_url: https://openrouter.ai/api/v1
  api_key: ${file:/run/credentials/vahub.service/llm_key}
  model: anthropic/claude-3.5-haiku
  temperature: 0.2
  max_tokens: 1024

budgets:
  iterations_per_turn: 8
  tool_result_bytes: 16384
  tokens_per_turn: 20000
  tokens_per_day: 300000
  wall_clock_text_s: 30
  wall_clock_voice_s: 8

speech:
  stt: { provider: browser }
  tts: { provider: browser }

policy:
  default: deny
  confirm_ttl_s: 60
  principals:
    agent:     { confirm: [destructive] }
    scheduler: { deny: ["*lock*", "*unlock*"] }
  rules:
    time.get_current_time:
      class: read
      constraints:
        tz: { max_len: 64 }
    homeassistant.get_state:
      class: read
      constraints:
        entity_id: { matches: "(light|sensor|lock)\\.[a-z0-9_]+", max_len: 64 }
    homeassistant.light_turn_on:
      class: write
      constraints:
        entity_id:      { matches: "^light\\.(kitchen|bedroom|hall)$" }
        brightness_pct: { range: [1, 100] }
    homeassistant.light_turn_off:
      class: write
      constraints:
        entity_id: { matches: "^light\\.(kitchen|bedroom|hall)$" }
    homeassistant.lock_unlock:
      class: destructive
      constraints:
        entity_id: { in: ["lock.front_door"] }

schedules:
  - id: morning
    cron: "30 6 * * 1-5"
    steps:
      - module: homeassistant
        tool: light_turn_on
        args: { entity_id: light.bedroom, brightness_pct: 40 }
```

An annotated version of this file ships as [examples/vahub.yaml](../examples/vahub.yaml).

## Checking a change

```bash
vahub doctor          # validate the config and every installed manifest
```

Configuration is read once, at startup. Changing the file, including policy rules and schedules, takes
effect on restart.
