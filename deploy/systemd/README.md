# Running vahub under systemd

Three units live here.

| Unit | What it does |
| --- | --- |
| `vahub.service` | the hub itself, sandboxed |
| `vahub-backup.service` | one consistent copy of the SQLite database |
| `vahub-backup.timer` | runs the backup daily |

The layout they assume:

```
/opt/vahub/venv          the installed package (not writable by the service user)
/etc/vahub/vahub.yaml    configuration, world readable, no secrets in it
/etc/vahub/modules.d/    module manifests, written by `vahub module add`
/etc/vahub/credentials/  secret files, root only, loaded as systemd credentials
/var/lib/vahub/          database, audit log, per module virtual environments
/var/backups/vahub/      daily database copies
```

## Install

Everything below is run as root.

### 1. The service account

```
useradd --system --home-dir /var/lib/vahub --shell /usr/sbin/nologin vahub
```

Or, if the distribution uses systemd-sysusers, drop this into
`/etc/sysusers.d/vahub.conf` and run `systemd-sysusers`:

```
u vahub - "vahub service" /var/lib/vahub /usr/sbin/nologin
```

The unit uses a fixed account rather than `DynamicUser=yes`. Two reasons: the
state directory outlives any single start, and a module manifest may name a
`runtime.user` for the hub to drop to, which needs uids that do not change
between restarts.

### 2. The package

```
python3.12 -m venv /opt/vahub/venv
/opt/vahub/venv/bin/pip install --upgrade pip
/opt/vahub/venv/bin/pip install vahub
```

Installing from a checkout instead:

```
/opt/vahub/venv/bin/pip install /path/to/vahub
```

The virtual environment is owned by root and the service user only reads it. A
service that can rewrite its own code is one bug away from being permanent.

### 3. Directories and configuration

```
install -d -m 0755 /etc/vahub /etc/vahub/modules.d
install -d -m 0700 /etc/vahub/credentials
install -d -o vahub -g vahub -m 0750 /var/backups/vahub
install -m 0644 examples/vahub.yaml /etc/vahub/vahub.yaml
```

`/var/lib/vahub` is not created by hand: `StateDirectory=vahub` creates it 0700
and owned by the service user on first start.

Edit `/etc/vahub/vahub.yaml`. The one line that has to change for systemd is the
model key, which the example points at the Docker secret path:

```yaml
llm:
  api_key: ${file:/run/credentials/vahub.service/llm_api_key}
```

That is where `LoadCredential=` puts the file. The path is stable, it is
`$CREDENTIALS_DIRECTORY` for this unit, and it exists only inside the unit's
mount namespace, so nothing else on the machine can read it.

### 4. Secrets

The model key, in plain text, readable only by root:

```
install -m 0600 /dev/null /etc/vahub/credentials/llm_api_key
printf '%s' 'sk-...' > /etc/vahub/credentials/llm_api_key
```

If the machine has a TPM, encrypt it so that a stolen disk is not a stolen key.
Feed the key on stdin:

```
systemd-creds encrypt --name=llm_api_key - /etc/vahub/credentials/llm_api_key
```

An encrypted credential needs the other directive, so change that line in the
unit as well:

```
LoadCredentialEncrypted=llm_api_key:/etc/vahub/credentials/llm_api_key
```

The path the service sees is the same either way, so the config does not
change.

Module credentials are different. A module receives its configuration through
the hub's environment, and the supervisor passes it only the keys its manifest
declares, so those values have to be environment variables rather than files.
They go in `/etc/vahub/module.env`, which the unit reads if it exists:

```
install -m 0600 /dev/null /etc/vahub/module.env
cat >> /etc/vahub/module.env <<'EOF'
HA_URL=https://homeassistant.lan:8123
HA_TOKEN=...
EOF
```

That file is read by PID 1 as root, never by the service user, and the values
land in the environment of the hub and of the modules that asked for them.

### 5. The units

```
install -m 0644 deploy/systemd/vahub.service /etc/systemd/system/
install -m 0644 deploy/systemd/vahub-backup.service /etc/systemd/system/
install -m 0644 deploy/systemd/vahub-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now vahub.service
systemctl enable --now vahub-backup.timer
```

Check it:

```
systemctl status vahub.service
journalctl -u vahub.service -f
curl -s http://127.0.0.1:8080/health
```

## Getting to it from another machine

The hub listens on loopback and authenticates nobody. Put a reverse proxy in
front of it that terminates TLS and requires a client certificate;
`deploy/docker/Caddyfile` is a working example of exactly that, and it applies
unchanged to a Caddy installed from a package. Set `web.auth_subject_header` to
whatever header the proxy sets, and make sure the proxy replaces that header
rather than passing through what the client sent.

Do not open `web.host` to `0.0.0.0` and stop there. Anyone who reaches the port
can drive the assistant with the permissions your policy grants.

## Backups

`vahub-backup.service` runs `VACUUM INTO`, which is the supported way to copy a
live SQLite database. A plain `cp` of `vahub.db` while the hub is running can
miss transactions still in the write ahead log, or capture a page torn by a
checkpoint. The copies are named by date and pruned after fourteen days.

Run one now:

```
systemctl start vahub-backup.service
journalctl -u vahub-backup.service -n 20
```

Restore:

```
systemctl stop vahub.service
install -o vahub -g vahub -m 0600 /var/backups/vahub/vahub-2026-01-31.db /var/lib/vahub/vahub.db
rm -f /var/lib/vahub/vahub.db-wal /var/lib/vahub/vahub.db-shm
systemctl start vahub.service
```

Removing the stale write ahead log matters. It belongs to the database you just
replaced, and SQLite will happily apply it to the new one.

## Upgrading

```
systemctl stop vahub.service
/opt/vahub/venv/bin/pip install --upgrade vahub
systemctl start vahub.service
```

Take a backup first (`systemctl start vahub-backup.service`), read the release
notes for schema or config changes, and if a config key was renamed the hub will
refuse to start and tell you which key it was. That is intentional: an unknown
key is a typo, and a silently ignored typo in `policy` is an open door.

Module code is upgraded separately, with `vahub module add name --version X`,
because a module is a separate program with its own release cycle.

## About the sandbox

The unit denies by default: read only filesystem apart from the state
directory, no capabilities, no new privileges, a system call allow list, and
only the address families that name resolution and HTTP actually need. Check
what the running system thinks of it with:

```
systemd-analyze security vahub.service
```

Two lines are worth knowing about before a module fails mysteriously.

`MemoryDenyWriteExecute=yes` breaks anything that generates code at runtime, a
JIT or a libffi closure for instance. CPython itself does not, and neither do
the hub's dependencies, but a third party module might.

`CapabilityBoundingSet=` is empty, which means the hub cannot become another
user. If you want a module to run under its own account (`runtime.user` in its
manifest), the unit needs `CAP_SETUID` and `CAP_SETGID` in both
`CapabilityBoundingSet=` and `AmbientCapabilities=`. The comment in the unit
shows the exact lines. Granting them lets the hub switch to those accounts, and
only those, since `NoNewPrivileges=yes` still forbids gaining anything more.
