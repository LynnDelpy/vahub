## What this changes

<!-- One paragraph. What was wrong or missing, and what this does about it. -->

Fixes #

## How it was verified

<!--
The commands you ran and what you observed. "Tests pass" on its own is not
useful; say which test covers the new behaviour, or why none does.
-->

## Effect on the security boundary

<!--
Answer even if the answer is "none". Anything that touches the list below needs
a sentence here.
-->

- [ ] This does not change what the policy gate allows, or the change is described above.
- [ ] This does not add a path that reaches a module without passing through the gate.
- [ ] This does not widen the environment a module process receives.
- [ ] Module output rendered on the assistant page is inserted as text, never as HTML.
- [ ] No new subprocess is started through a shell, and no argument is interpolated into a command string.
- [ ] Secrets stay out of logs, out of the audit trail, and out of error messages returned to callers.

## Checklist

- [ ] `ruff check .` and `ruff format --check .` are clean.
- [ ] `mypy` is clean.
- [ ] `pytest` passes, and new behaviour has a test.
- [ ] Documentation is updated if the configuration, the CLI or the module contract changed.
- [ ] `CHANGELOG.md` has an entry under Unreleased, unless this is an internal change with no user visible effect.
- [ ] Commits follow the convention in `CONTRIBUTING.md`.
