"""Installing a module: resolve, materialise, build a venv, validate, publish.

A module is not a plugin: it is somebody else's program, installed into its own
virtual environment and run as its own process. Installing one is therefore a
small package manager rather than a copy, and it makes two promises.

* Everything is built in a staging directory under `<state>/modules` and moved
  into place only after the module's own `module.yaml` has validated. An
  interrupted or failed install leaves the previous version running rather than
  a half-built venv the hub would try to spawn.
* The manifest is written last, because the hub discovers modules by manifest.
  Publishing the manifest is what makes a module exist, and by then the code it
  points at is complete.

Nothing here runs a shell. Every subprocess is an argv list with a timeout, and
the installer refuses to run as root unless told otherwise, because `pip
install` executes arbitrary code from the package being installed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from vahub.__about__ import __version__
from vahub.contracts.manifest import NAME_RE, Manifest
from vahub.contracts.registry import (
    GitSource,
    PathSource,
    PypiSource,
    Source,
    parse_source_spec,
)
from vahub.modules.registry_client import RegistryClient, RegistryError
from vahub.modules.store import InstalledModule, InstallRecord, ModuleStore, StoreError, describe_source

if TYPE_CHECKING:
    from vahub.config.models import Config

MANIFEST_FILENAME = "module.yaml"
PROJECT_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg")

# Copied source trees keep only what is needed to build and to read the
# manifest; the rest is noise that would be installed and then never used.
COPY_IGNORE = shutil.ignore_patterns(
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "*.pyc",
    ".venv",
    "venv",
    ".tox",
    ".nox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
)

DEFAULT_STEP_TIMEOUT_S = 600.0
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_VERSION_PART = re.compile(r"\d+")


class InstallError(Exception):
    """Raised with a message meant for a human at a terminal."""


@dataclass
class InstallResult:
    name: str
    version: str
    source: Source
    manifest: Manifest
    manifest_path: Path
    module_dir: Path
    venv: Path | None
    changed: bool = True
    previous_version: str | None = None
    required_config: list[str] = field(default_factory=list)
    optional_config: list[str] = field(default_factory=list)
    missing_config: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def upgraded(self) -> bool:
        return self.previous_version is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "source": describe_source(self.source),
            "manifest_path": str(self.manifest_path),
            "module_dir": str(self.module_dir),
            "venv": str(self.venv) if self.venv else None,
            "changed": self.changed,
            "previous_version": self.previous_version,
            "required_config": self.required_config,
            "optional_config": self.optional_config,
            "missing_config": self.missing_config,
            "warnings": self.warnings,
        }


class Installer:
    """install / upgrade / remove for one hub configuration."""

    def __init__(
        self,
        config: Config,
        *,
        registry: RegistryClient | None = None,
        store: ModuleStore | None = None,
        allow_root: bool = False,
        offline: bool = False,
        step_timeout_s: float = DEFAULT_STEP_TIMEOUT_S,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        self._config = config
        self._registry = registry if registry is not None else RegistryClient.from_config(config)
        self._store = store if store is not None else ModuleStore.from_config(config)
        self._allow_root = allow_root
        self._offline = offline
        self._step_timeout_s = step_timeout_s
        self._on_progress = on_progress
        # Resolved once so venv creation and package installation cannot
        # disagree about which tool built the environment.
        self._uv = shutil.which("uv")

    @property
    def store(self) -> ModuleStore:
        return self._store

    @property
    def registry(self) -> RegistryClient:
        return self._registry

    # --- public API --------------------------------------------------------
    def install(
        self,
        name: str | None = None,
        *,
        version: str | None = None,
        source_spec: str | None = None,
        force: bool = False,
    ) -> InstallResult:
        """Install by registry name, by explicit source, or both (a source with
        a name is how a module is pinned to a fork)."""
        self._require_non_root()
        if not name and not source_spec:
            raise InstallError("give a module name or a --source")
        if name and not NAME_RE.match(name):
            raise InstallError(f"invalid module name {name!r}: lowercase letters, digits, _ and - only")

        requires: list[str] = []
        optional: list[str] = []
        warnings: list[str] = []
        registry_url: str | None = None
        resolved_version: str | None = version

        if source_spec:
            try:
                source = parse_source_spec(source_spec)
            except ValueError as e:
                raise InstallError(f"invalid --source {source_spec!r}: {e}") from e
        else:
            assert name is not None
            try:
                resolved_version, entry = self._registry.resolve(name, version, offline=self._offline)
            except RegistryError as e:
                raise InstallError(str(e)) from e
            source = entry.source
            requires = list(entry.requires_config)
            optional = list(entry.optional_config)
            registry_url = self._registry.url
            warnings.extend(self._registry.warnings)
            if entry.notes:
                warnings.append(entry.notes)
            self._check_hub_version(name, entry.requires_vahub)

        previous = self._store.get(name) if name else None
        if previous is not None and not force:
            unchanged = self._already_installed(previous, source, resolved_version)
            if unchanged is not None:
                return unchanged

        return self._run_install(
            source=source,
            name=name,
            version=resolved_version,
            source_spec=source_spec,
            registry_url=registry_url,
            requires_config=requires,
            optional_config=optional,
            warnings=warnings,
        )

    def upgrade(self, name: str, *, version: str | None = None) -> InstallResult:
        """Reinstall from the registry, or from the source the module came from
        when it is not in the registry at all."""
        self._require_non_root()
        if not self._store.is_installed(name):
            raise InstallError(f"module {name!r} is not installed")
        record = self._store.read_record(name)
        try:
            return self.install(name, version=version, force=True)
        except InstallError:
            if record is None or version is not None:
                raise
            self._say(f"{name} is not in the registry; reinstalling from {describe_source(record.source)}")
            return self._run_install(
                source=record.source,
                name=name,
                version=None,
                source_spec=record.source_spec,
                registry_url=record.registry_url,
                requires_config=[],
                optional_config=[],
                warnings=[f"{name} is not in the registry; reinstalled from its recorded source"],
            )

    def remove(self, name: str) -> list[Path]:
        installed = self._store.get(name)
        if installed is None:
            raise InstallError(f"module {name!r} is not installed")
        try:
            return self._store.remove(name)
        except StoreError as e:
            raise InstallError(str(e)) from e

    def list_installed(self) -> list[InstalledModule]:
        return self._store.list_installed()

    # --- the install itself ------------------------------------------------
    def _run_install(
        self,
        *,
        source: Source,
        name: str | None,
        version: str | None,
        source_spec: str | None,
        registry_url: str | None,
        requires_config: Sequence[str],
        optional_config: Sequence[str],
        warnings: Sequence[str],
    ) -> InstallResult:
        staging = self._staging_dir()
        collected = list(warnings)
        try:
            tree = self._materialise(source, staging)
            installable = tree is not None and any((tree / m).is_file() for m in PROJECT_MARKERS)

            # A source tree carries its manifest, so it can be read and rejected
            # before a venv is built. Only a pypi source has to be installed
            # first, because there is nothing to read until it is.
            manifest: Manifest | None = None
            if tree is not None:
                found = self._find_manifest(tree, None, None)
                if found is not None:
                    manifest = self._checked_manifest(found, name)

            venv: Path | None = None
            if isinstance(source, PypiSource) or installable:
                venv = staging / "venv"
                self._create_venv(venv)
                target = (
                    f"{source.package}=={source.version}" if isinstance(source, PypiSource) else str(tree)
                )
                self._install_into_venv(venv, [target])
            else:
                collected.append(
                    "the source contains no Python project; only the manifest was installed, "
                    "so its runtime command must not rely on {venv}"
                )

            if manifest is None:
                package = source.package if isinstance(source, PypiSource) else None
                manifest = self._checked_manifest(self._locate_manifest(tree, venv, package), name)
            resolved_name = manifest.name
            previous = self._store.get(resolved_name)
            previous_version = previous.version if previous is not None else None

            module_dir = self._publish(resolved_name, staging)
            venv_final = module_dir / "venv" if venv is not None else None

            record = InstallRecord(
                name=resolved_name,
                version=version or manifest.version,
                source=source,
                installed_at=time.time(),
                source_spec=source_spec,
                registry_url=registry_url,
                python=sys.executable,
                has_venv=venv_final is not None,
            )
            self._store.write_record(record)
            manifest_path = self._write_manifest(resolved_name, manifest)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        collected.extend(self._sanity_warnings(manifest, module_dir, venv_final))

        required = _unique([*manifest.config.required, *requires_config])
        optional_keys = _unique([*manifest.config.optional, *optional_config])
        result = InstallResult(
            name=resolved_name,
            version=record.version,
            source=source,
            manifest=manifest,
            manifest_path=manifest_path,
            module_dir=module_dir,
            venv=venv_final,
            changed=True,
            previous_version=previous_version,
            required_config=required,
            optional_config=optional_keys,
            missing_config=[key for key in required if not os.environ.get(key)],
            warnings=collected,
        )
        self._say(f"installed {result.name} {result.version}")
        return result

    def _already_installed(
        self, previous: InstalledModule, source: Source, version: str | None
    ) -> InstallResult | None:
        """Repeat installs are common (a config-management run, a retried
        command). When nothing would change, do nothing and say so."""
        record = previous.record
        if record is None or previous.manifest is None:
            return None
        if isinstance(source, PathSource):
            return None  # a local checkout changes under us; always rebuild
        if record.source != source:
            return None
        if version is not None and record.version != version:
            return None
        required = list(previous.manifest.config.required)
        return InstallResult(
            name=previous.name,
            version=previous.version,
            source=record.source,
            manifest=previous.manifest,
            manifest_path=previous.manifest_path,
            module_dir=previous.module_dir,
            venv=previous.venv if record.has_venv else None,
            changed=False,
            required_config=required,
            optional_config=list(previous.manifest.config.optional),
            missing_config=previous.missing_config(),
        )

    # --- step 2: materialise the source ------------------------------------
    def _materialise(self, source: Source, staging: Path) -> Path | None:
        """Put the module's source tree at <staging>/src. Returns None for a
        pypi source, which has no tree until it is installed."""
        if isinstance(source, PypiSource):
            _check_argument("package name", source.package)
            _check_argument("version", source.version)
            return None
        destination = staging / "src"
        if isinstance(source, PathSource):
            origin = Path(source.path).expanduser()
            try:
                origin = origin.resolve(strict=True)
            except OSError as e:
                raise InstallError(f"source path {source.path} is not readable: {e}") from e
            if not origin.is_dir():
                raise InstallError(f"source path {origin} is not a directory")
            self._say(f"copying {origin}")
            shutil.copytree(origin, destination, ignore=COPY_IGNORE, symlinks=True)
            return destination
        return self._git_checkout(source, staging, destination)

    def _git_checkout(self, source: GitSource, staging: Path, destination: Path) -> Path:
        for label, value in (("url", source.url), ("rev", source.rev), ("subdir", source.subdir)):
            _check_argument(label, value)
        clone = staging / "clone"
        clone.mkdir(parents=True)
        self._say(f"cloning {source.url} at {source.rev}")
        self._run(["git", "init", "--quiet", str(clone)], what="git init")
        self._run(["git", "-C", str(clone), "remote", "add", "origin", source.url], what="git remote add")
        try:
            # A shallow fetch of exactly the pinned rev is the cheap path.
            self._run(
                ["git", "-C", str(clone), "fetch", "--quiet", "--depth", "1", "origin", source.rev],
                what="git fetch",
            )
            self._run(["git", "-C", str(clone), "checkout", "--quiet", "FETCH_HEAD"], what="git checkout")
        except InstallError:
            # Servers that refuse to serve an arbitrary sha, and older ones that
            # refuse a shallow fetch by tag, need the whole history.
            self._say("shallow fetch refused, retrying with a full clone")
            self._run(["git", "-C", str(clone), "fetch", "--quiet", "--tags", "origin"], what="git fetch")
            self._run(
                ["git", "-C", str(clone), "checkout", "--quiet", "--detach", source.rev],
                what="git checkout",
            )

        # Always resolve and report the commit that was actually checked out. A
        # git tag is not immutable: it can be moved to point at different code
        # after it was first reviewed. Reporting the resolved commit in the
        # install output turns "installed the tag" into "installed this exact
        # commit", so an operator watching the install has the sha to record.
        # When the rev is itself a full sha, the resolved HEAD must equal it.
        head = self._run(["git", "-C", str(clone), "rev-parse", "HEAD"], what="git rev-parse")
        resolved_commit = head.stdout.strip()
        self._say(f"resolved {source.rev} to commit {resolved_commit}")
        if _SHA_RE.match(source.rev) and resolved_commit != source.rev:
            raise InstallError(f"checked out {resolved_commit} but the source pins {source.rev}")

        root = clone if not source.subdir else clone / source.subdir
        resolved = root.resolve()
        if resolved != clone.resolve() and not resolved.is_relative_to(clone.resolve()):
            raise InstallError(f"subdir {source.subdir!r} escapes the repository")
        if not resolved.is_dir():
            raise InstallError(f"subdir {source.subdir!r} does not exist in {source.url}")

        shutil.copytree(resolved, destination, ignore=COPY_IGNORE, symlinks=True)
        shutil.rmtree(clone, ignore_errors=True)  # the history is not part of the install
        return destination

    # --- step 3 and 4: the venv --------------------------------------------
    def _create_venv(self, venv: Path) -> None:
        self._say(f"creating {venv.name} with {'uv' if self._uv else 'python -m venv'}")
        if self._uv:
            self._run([self._uv, "venv", "--python", sys.executable, str(venv)], what="uv venv")
        else:
            self._run([sys.executable, "-m", "venv", str(venv)], what="python -m venv")

    def _install_into_venv(self, venv: Path, targets: list[str]) -> None:
        python = venv / "bin" / "python"
        self._say(f"installing {', '.join(targets)}")
        if self._uv:
            cmd = [self._uv, "pip", "install", "--python", str(python), *targets]
        else:
            cmd = [
                str(venv / "bin" / "pip"),
                "install",
                "--disable-pip-version-check",
                "--no-input",
                *targets,
            ]
        self._run(cmd, what="installing the module")

    # --- step 5: the module's own manifest ---------------------------------
    def _find_manifest(
        self,
        tree: Path | None,
        venv: Path | None,
        package: str | None,
        looked: list[str] | None = None,
    ) -> Path | None:
        """The module's own module.yaml, in its source tree or in the package it
        installed. Ambiguity is an error; absence is for the caller to judge."""
        candidates: list[Path] = []
        seen = looked if looked is not None else []
        if tree is not None:
            seen.append(str(tree / MANIFEST_FILENAME))
            direct = tree / MANIFEST_FILENAME
            if direct.is_file():
                return direct
            candidates.extend(sorted(tree.glob(f"*/{MANIFEST_FILENAME}")))
        if venv is not None:
            for site in sorted(venv.glob("lib/python*/site-packages")):
                seen.append(str(site / f"*/{MANIFEST_FILENAME}"))
                candidates.extend(sorted(site.glob(MANIFEST_FILENAME)))
                candidates.extend(
                    p for p in sorted(site.glob(f"*/{MANIFEST_FILENAME}")) if not _is_metadata(p)
                )
        if not candidates:
            return None
        if package is not None and len(candidates) > 1:
            wanted = package.replace("-", "_").lower()
            preferred = [p for p in candidates if p.parent.name.lower() == wanted]
            if preferred:
                return preferred[0]
        if len({p.resolve() for p in candidates}) > 1:
            listed = ", ".join(str(p) for p in candidates)
            raise InstallError(f"found more than one {MANIFEST_FILENAME}: {listed}")
        return candidates[0]

    def _locate_manifest(self, tree: Path | None, venv: Path | None, package: str | None) -> Path:
        looked: list[str] = []
        found = self._find_manifest(tree, venv, package, looked)
        if found is None:
            raise InstallError(f"the source has no {MANIFEST_FILENAME}. Looked in: {', '.join(looked)}")
        return found

    def _checked_manifest(self, path: Path, name: str | None) -> Manifest:
        manifest = self._read_manifest(path)
        if name and manifest.name != name:
            raise InstallError(
                f"the source declares module name {manifest.name!r}, not {name!r}; "
                f"install it as `vahub module add {manifest.name}` or fix its {MANIFEST_FILENAME}"
            )
        return manifest

    def _read_manifest(self, path: Path) -> Manifest:
        try:
            manifest = Manifest.from_file(path)
        except Exception as e:  # any parse or validation failure is reported as text
            raise InstallError(f"{path} is not a valid module manifest:\n  {e}") from e
        if not NAME_RE.match(manifest.name):
            raise InstallError(f"invalid module name {manifest.name!r} in {path}")
        return manifest

    # --- step 6: publish ---------------------------------------------------
    def _publish(self, name: str, staging: Path) -> Path:
        """Move the finished staging directory into place, keeping the previous
        install until the move has succeeded."""
        final = self._store.module_dir(name)
        final.parent.mkdir(parents=True, exist_ok=True)
        backup: Path | None = None
        if final.exists():
            backup = final.with_name(f".{name}.previous-{uuid.uuid4().hex[:8]}")
            final.rename(backup)
        try:
            staging.rename(final)
        except OSError as e:
            if backup is not None:
                backup.rename(final)
            raise InstallError(f"cannot move the new module into {final}: {e}") from e

        try:
            # Scripts in a venv carry the absolute path of the venv that built
            # them, and this one was built in staging.
            self._rewrite_shebangs(final / "venv" / "bin", staging, final)
        except Exception:
            if backup is not None:
                shutil.rmtree(final, ignore_errors=True)
                backup.rename(final)
            raise
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
        return final

    def _write_manifest(self, name: str, manifest: Manifest) -> Path:
        path = self._store.manifest_path(name)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".yaml.tmp")
            tmp.write_text(manifest.to_yaml())
            tmp.replace(path)  # the hub never reads a half-written manifest
        except OSError as e:
            raise InstallError(f"cannot write the manifest to {path}: {e}") from e
        return path

    @staticmethod
    def _rewrite_shebangs(bin_dir: Path, old_root: Path, new_root: Path) -> None:
        if not bin_dir.is_dir():
            return
        old = f"#!{old_root}".encode()
        new = f"#!{new_root}".encode()
        for entry in bin_dir.iterdir():
            if entry.is_symlink() or not entry.is_file():
                continue
            try:
                data = entry.read_bytes()
            except OSError:
                continue
            if not data.startswith(b"#!"):
                continue
            head, sep, tail = data.partition(b"\n")
            if old not in head:
                continue
            try:
                entry.write_bytes(head.replace(old, new) + sep + tail)
            except OSError:
                continue

    # --- checks and helpers ------------------------------------------------
    def _sanity_warnings(self, manifest: Manifest, module_dir: Path, venv: Path | None) -> list[str]:
        """Catch the manifest that points at an interpreter nobody built. It is
        the failure mode that hides best: the install succeeds, and the module
        only fails to spawn later."""
        out: list[str] = []
        expanded = manifest.expand(
            venv=venv or module_dir / "venv",
            state=self._config.hub.state_dir,
            config=self._config.hub.modules_dir.parent,
        )
        executable = expanded.runtime.command[0]
        if executable.startswith("/") and not Path(executable).exists():
            out.append(f"runtime.command points at {executable}, which does not exist")
        if not manifest.tools:
            out.append("the manifest declares no tools; the hub will offer none from this module")
        return out

    def _check_hub_version(self, name: str, requires: str | None) -> None:
        if not requires:
            return
        wanted, have = _version_tuple(requires), _version_tuple(__version__)
        if wanted is None or have is None:
            return
        if have < wanted:
            raise InstallError(
                f"{name} requires vahub {requires} or newer, this is {__version__}; upgrade vahub first"
            )

    def _staging_dir(self) -> Path:
        root = self._store.root
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise InstallError(f"cannot create {root}: {e}") from e
        # Staged inside the store so the final move is a rename on one
        # filesystem, which is atomic, rather than a copy that can half happen.
        # An install that was killed outright leaves its staging directory
        # behind; clear the old ones before adding another.
        self._store.prune_incomplete()
        staging = root / f".staging-{uuid.uuid4().hex[:12]}"
        staging.mkdir()
        return staging

    def _require_non_root(self) -> None:
        if hasattr(os, "geteuid") and os.geteuid() == 0 and not self._allow_root:
            raise InstallError(
                "refusing to install as root: building a module runs code from the package "
                "being installed. Run as the user that owns the state directory, or pass "
                "--allow-root if that user really is root."
            )

    def _say(self, message: str) -> None:
        if self._on_progress is not None:
            self._on_progress(message)

    def _run(self, cmd: list[str], *, what: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(  # an argv list, never a shell string
                cmd,
                cwd=str(cwd) if cwd else None,
                env=self._child_env(),
                capture_output=True,
                text=True,
                timeout=self._step_timeout_s,
                check=False,
            )
        except FileNotFoundError as e:
            raise InstallError(f"{what}: {cmd[0]} is not installed") from e
        except subprocess.TimeoutExpired as e:
            raise InstallError(f"{what}: timed out after {self._step_timeout_s:.0f}s") from e
        except OSError as e:
            raise InstallError(f"{what}: {e}") from e
        if proc.returncode != 0:
            output = _tail(proc.stderr or proc.stdout)
            raise InstallError(f"{what} failed (exit {proc.returncode}):\n{output}")
        return proc

    @staticmethod
    def _child_env() -> dict[str, str]:
        """A minimal environment, built up rather than inherited. VIRTUAL_ENV in
        particular must not leak in: uv would then install into the hub's own
        environment instead of the module's."""
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            # Nothing here may stop to ask a question: there is no terminal to
            # answer on when this runs from a config-management tool.
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        passthrough = (
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "REQUESTS_CA_BUNDLE",
            "CURL_CA_BUNDLE",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
            "PIP_INDEX_URL",
            "PIP_EXTRA_INDEX_URL",
            "PIP_CACHE_DIR",
            "PIP_FIND_LINKS",
            "UV_CACHE_DIR",
            "UV_INDEX_URL",
            "UV_EXTRA_INDEX_URL",
            "UV_PYTHON",
            "XDG_CACHE_HOME",
            "TMPDIR",
            "SSH_AUTH_SOCK",
            "GIT_SSH_COMMAND",
        )
        for key in passthrough:
            if key in os.environ:
                env[key] = os.environ[key]
        return env


def _check_argument(label: str, value: str | None) -> None:
    # A value beginning with "-" would be read as an option by git or pip. The
    # source of these strings is a registry index, so it is not ours to trust.
    if value is None:
        return
    if value.startswith("-"):
        raise InstallError(f"refusing a {label} that starts with '-': {value!r}")
    if "\n" in value or "\x00" in value:
        raise InstallError(f"invalid {label}: {value!r}")


def _is_metadata(path: Path) -> bool:
    return path.parent.name.endswith((".dist-info", ".egg-info", ".data"))


def _unique(values: Sequence[str]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return list(seen)


def _version_tuple(text: str) -> tuple[int, ...] | None:
    parts = _VERSION_PART.findall(text)
    return tuple(int(p) for p in parts[:3]) if parts else None


def _tail(text: str, lines: int = 20) -> str:
    kept = [line for line in (text or "").strip().splitlines() if line.strip()][-lines:]
    return "\n".join(f"  {line}" for line in kept) or "  (no output)"
