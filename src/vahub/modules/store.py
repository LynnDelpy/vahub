"""What is installed on this machine, and how to take it away again.

An installed module is two things in two places: a manifest in
`hub.modules_dir` (which is what the hub reads at startup) and a directory under
`<state>/modules/<name>` holding its venv, a copy of its source and one
`install.json`. The split is deliberate: the manifest is configuration a person
may read and back up, the rest is machine state that can be deleted and rebuilt.

`install.json` records what the manifest deliberately does not carry, namely
where the code came from and when it arrived, so `vahub module list` can answer
"where did this come from" without a network call.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vahub.__about__ import __version__
from vahub.contracts.manifest import NAME_RE, Manifest
from vahub.contracts.registry import GitSource, PathSource, PypiSource, Source

if TYPE_CHECKING:
    from vahub.config.models import Config

RECORD_FILENAME = "install.json"
RECORD_SCHEMA_VERSION = 1
VENV_DIRNAME = "venv"
SRC_DIRNAME = "src"


class StoreError(Exception):
    """Raised with a message meant for a human at a terminal."""


class InstallRecord(BaseModel):
    """The provenance of one installed module."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = RECORD_SCHEMA_VERSION
    name: str
    version: str
    source: Source = Field(discriminator="type")
    installed_at: float
    # What the user typed, when they typed a --source. Kept verbatim so `module
    # list` can show the same string back and an upgrade can repeat it.
    source_spec: str | None = None
    registry_url: str | None = None
    vahub_version: str = __version__
    python: str | None = None
    has_venv: bool = True

    @property
    def installed_at_iso(self) -> str:
        return datetime.fromtimestamp(self.installed_at, UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class InstalledModule:
    name: str
    manifest_path: Path
    module_dir: Path
    venv: Path
    manifest: Manifest | None = None
    record: InstallRecord | None = None
    # Set when the manifest is present but unreadable. Listing must still work:
    # a broken manifest is the thing the user needs to be told about, not a
    # traceback.
    manifest_error: str | None = None

    @property
    def version(self) -> str:
        if self.record is not None:
            return self.record.version
        if self.manifest is not None:
            return self.manifest.version
        return "unknown"

    @property
    def source(self) -> Source | None:
        return self.record.source if self.record is not None else None

    @property
    def source_label(self) -> str:
        if self.record is not None and self.record.source_spec:
            return self.record.source_spec
        return describe_source(self.record.source) if self.record is not None else "unknown"

    @property
    def installed_at_iso(self) -> str | None:
        return self.record.installed_at_iso if self.record is not None else None

    @property
    def complete(self) -> bool:
        """A module the hub can actually start: a manifest it could read, and
        the directory the manifest's paths point into."""
        return self.manifest is not None and self.module_dir.is_dir()

    def missing_config(self, env: Mapping[str, str] | None = None) -> list[str]:
        """Required config keys that are not in the environment. The hub passes
        only declared keys to a module, so an unset one means it cannot start."""
        if self.manifest is None:
            return []
        environ = os.environ if env is None else env
        return [key for key in self.manifest.config.required if not environ.get(key)]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.manifest.description if self.manifest else "",
            "source": self.source_label,
            "installed_at": self.installed_at_iso,
            "manifest_path": str(self.manifest_path),
            "module_dir": str(self.module_dir),
            "venv": str(self.venv),
            "complete": self.complete,
            "manifest_error": self.manifest_error,
            "missing_config": self.missing_config(),
        }


def describe_source(source: Source) -> str:
    """Render a source the way `--source` would accept it back."""
    if isinstance(source, GitSource):
        text = f"git+{source.url}@{source.rev}"
        return f"{text}#subdir={source.subdir}" if source.subdir else text
    if isinstance(source, PypiSource):
        return f"pypi:{source.package}=={source.version}"
    if isinstance(source, PathSource):
        return source.path
    return str(source)


class ModuleStore:
    """The installed-module directory: locations, records, removal."""

    def __init__(self, state_dir: Path, modules_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.modules_dir = Path(modules_dir)

    @classmethod
    def from_config(cls, config: Config) -> ModuleStore:
        return cls(config.hub.state_dir, config.hub.modules_dir)

    # --- locations ---------------------------------------------------------
    @property
    def root(self) -> Path:
        return self.state_dir / "modules"

    def module_dir(self, name: str) -> Path:
        return self.root / self._checked(name)

    def venv_dir(self, name: str) -> Path:
        return self.module_dir(name) / VENV_DIRNAME

    def venv_python(self, name: str) -> Path:
        return self.venv_dir(name) / "bin" / "python"

    def src_dir(self, name: str) -> Path:
        return self.module_dir(name) / SRC_DIRNAME

    def manifest_path(self, name: str) -> Path:
        return self.modules_dir / f"{self._checked(name)}.yaml"

    def record_path(self, name: str) -> Path:
        return self.module_dir(name) / RECORD_FILENAME

    @staticmethod
    def _checked(name: str) -> str:
        # Names arrive from a registry index and from a module's own manifest,
        # neither of which is ours. Refusing anything but the contract's name
        # shape is what keeps "../../etc" out of a path join.
        if not NAME_RE.match(name or ""):
            raise StoreError(f"invalid module name {name!r}: lowercase letters, digits, _ and - only")
        return name

    # --- reading -----------------------------------------------------------
    def is_installed(self, name: str) -> bool:
        return self.manifest_path(name).is_file() or self.module_dir(name).is_dir()

    def read_record(self, name: str) -> InstallRecord | None:
        path = self.record_path(name)
        try:
            raw = path.read_text()
        except OSError:
            return None
        try:
            return InstallRecord.model_validate_json(raw)
        except ValidationError:
            # A record written by a future version, or a corrupted one. It is
            # metadata: losing it must not make a module unremovable.
            return None

    def write_record(self, record: InstallRecord) -> Path:
        path = self.record_path(record.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(record.model_dump_json(indent=2) + "\n")
        tmp.replace(path)
        return path

    def read_manifest(self, name: str) -> Manifest:
        path = self.manifest_path(name)
        if not path.is_file():
            raise StoreError(f"no manifest for module {name!r} at {path}")
        try:
            return Manifest.from_file(path)
        except Exception as e:  # the validation text is what the user needs
            raise StoreError(f"{path} is not a valid manifest:\n  {e}") from e

    def get(self, name: str) -> InstalledModule | None:
        if not self.is_installed(name):
            return None
        return self._describe(name)

    def list_installed(self) -> list[InstalledModule]:
        """Every module that has a manifest or a directory. The two can diverge
        (a half-removed install, a hand-written manifest), and a listing that
        hid either half would hide exactly the case that needs fixing."""
        names: set[str] = set()
        if self.modules_dir.is_dir():
            names.update(p.stem for p in self.modules_dir.glob("*.yaml") if NAME_RE.match(p.stem))
        if self.root.is_dir():
            names.update(
                p.name for p in self.root.iterdir() if p.is_dir() and NAME_RE.match(p.name)
            )
        return [self._describe(name) for name in sorted(names)]

    def _describe(self, name: str) -> InstalledModule:
        manifest: Manifest | None = None
        error: str | None = None
        path = self.manifest_path(name)
        if path.is_file():
            try:
                manifest = Manifest.from_file(path)
            except Exception as e:  # reported, not raised: listing must still work
                error = str(e)
        else:
            error = f"no manifest at {path}"
        return InstalledModule(
            name=name,
            manifest_path=path,
            module_dir=self.module_dir(name),
            venv=self.venv_dir(name),
            manifest=manifest,
            record=self.read_record(name),
            manifest_error=error,
        )

    # --- removal -----------------------------------------------------------
    def remove(self, name: str) -> list[Path]:
        """Delete the manifest and the module directory. Idempotent: removing
        half an install finishes the job instead of failing."""
        removed: list[Path] = []
        manifest = self.manifest_path(name)
        if manifest.is_file() or manifest.is_symlink():
            try:
                manifest.unlink()
            except OSError as e:
                raise StoreError(f"cannot remove {manifest}: {e}") from e
            removed.append(manifest)

        directory = self.module_dir(name)
        if directory.is_dir() and not directory.is_symlink():
            self._rmtree(directory)
            removed.append(directory)
        elif directory.is_symlink():
            directory.unlink()
            removed.append(directory)
        return removed

    def prune_incomplete(self, max_age_s: float = 86400.0) -> list[Path]:
        """Delete leftover staging and rollback directories. An install that was
        killed outright (a reboot, an OOM) cannot clean up after itself, and the
        next one should not have to step around the wreckage. Only the dotted
        working directories are touched; an installed module is never one."""
        removed: list[Path] = []
        if not self.root.is_dir():
            return removed
        cutoff = time.time() - max_age_s
        for path in sorted(self.root.iterdir()):
            if not path.name.startswith(".") or not path.is_dir() or path.is_symlink():
                continue
            if ".staging-" not in path.name and ".previous-" not in path.name:
                continue
            try:
                if path.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            with contextlib.suppress(StoreError):
                self._rmtree(path)
                removed.append(path)
        return removed

    def _rmtree(self, path: Path) -> None:
        # Deleting a tree is the one operation here that cannot be undone, so it
        # only ever happens strictly inside the store's own root.
        root = self.root.resolve()
        try:
            target = path.resolve()
        except OSError as e:
            raise StoreError(f"cannot resolve {path}: {e}") from e
        if target == root or not target.is_relative_to(root):
            raise StoreError(f"refusing to delete {target}: outside {root}")
        try:
            shutil.rmtree(target)
        except OSError as e:
            raise StoreError(f"cannot remove {target}: {e}") from e
