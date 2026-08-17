"""The module registry: how `vahub module add homeassistant` finds a module.

The registry is a plain JSON index, fetched over HTTPS or read from disk. It
maps a short name to a source. It is an *index*, not a store: the code lives
wherever its author keeps it, so a module can be part of the official
vahub-modules repository, in someone else's repository, or on a private server,
and all three install the same way.

    {
      "schema_version": 1,
      "modules": {
        "homeassistant": {
          "description": "Lights, locks and sensors via Home Assistant",
          "homepage": "https://github.com/LynnDelpy/vahub-modules",
          "tags": ["home-automation"],
          "latest": "0.2.0",
          "versions": {
            "0.2.0": {
              "source": {
                "type": "git",
                "url": "https://github.com/LynnDelpy/vahub-modules",
                "rev": "modules/homeassistant/v0.2.0",
                "subdir": "modules/homeassistant"
              },
              "requires_config": ["HA_URL", "HA_TOKEN"]
            }
          }
        }
      }
    }

Trust: installing a module runs its code on your machine with whatever
configuration you give it. The registry does not change that, it only makes it
convenient, so a source is always pinned to an immutable revision and a
third-party source can be installed directly without any registry at all:

    vahub module add --source git+https://example.com/mod.git@v1.2.3
    vahub module add --source ./my-module
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

REGISTRY_SCHEMA_VERSION = 1

# The official index. Overridable so an organisation can run its own.
DEFAULT_REGISTRY_URL = "https://raw.githubusercontent.com/LynnDelpy/vahub-modules/main/registry.json"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GitSource(Strict):
    type: Literal["git"] = "git"
    url: str
    # A tag or full commit sha. A branch is refused: "install whatever main
    # happens to be today" is not something to build a home on.
    rev: str
    subdir: str | None = None

    @field_validator("rev")
    @classmethod
    def _pinned(cls, v: str) -> str:
        if v in ("main", "master", "HEAD") or v.startswith("refs/heads/"):
            raise ValueError(
                f"rev {v!r} is a moving branch; pin a tag or commit sha so an install is reproducible"
            )
        return v


class PathSource(Strict):
    type: Literal["path"] = "path"
    path: str


class PypiSource(Strict):
    type: Literal["pypi"] = "pypi"
    package: str
    version: str


Source = GitSource | PathSource | PypiSource


class ModuleVersion(Strict):
    source: Source = Field(discriminator="type")
    # Config keys the module needs before it can start. The installer asks for
    # these, so a fresh install is usable without reading the module's source.
    requires_config: list[str] = Field(default_factory=list)
    optional_config: list[str] = Field(default_factory=list)
    # Minimum hub version this module is known to work with.
    requires_vahub: str | None = None
    notes: str | None = None


_SEMVERISH = re.compile(r"^\d+(\.\d+)*")


class ModuleEntry(Strict):
    description: str = ""
    homepage: str | None = None
    author: str | None = None
    tags: list[str] = Field(default_factory=list)
    latest: str | None = None
    versions: dict[str, ModuleVersion] = Field(default_factory=dict)

    def resolve(self, version: str | None = None) -> tuple[str, ModuleVersion]:
        """Pick a version: the requested one, else `latest`, else the highest
        numeric version present."""
        if version:
            if version not in self.versions:
                available = ", ".join(sorted(self.versions)) or "none"
                raise KeyError(f"version {version!r} not published (available: {available})")
            return version, self.versions[version]
        if self.latest and self.latest in self.versions:
            return self.latest, self.versions[self.latest]
        if not self.versions:
            raise KeyError("module has no published versions")

        def key(v: str) -> tuple:
            m = _SEMVERISH.match(v)
            return tuple(int(p) for p in m.group(0).split(".")) if m else (0,)

        newest = max(self.versions, key=key)
        return newest, self.versions[newest]


class Registry(Strict):
    schema_version: int = REGISTRY_SCHEMA_VERSION
    updated_at: str | None = None
    modules: dict[str, ModuleEntry] = Field(default_factory=dict)

    def search(self, query: str) -> list[tuple[str, ModuleEntry]]:
        q = query.lower().strip()
        hits = []
        for name, entry in sorted(self.modules.items()):
            haystack = " ".join([name, entry.description, " ".join(entry.tags)]).lower()
            if not q or q in haystack:
                hits.append((name, entry))
        return hits

    @field_validator("schema_version")
    @classmethod
    def _supported(cls, v: int) -> int:
        if v > REGISTRY_SCHEMA_VERSION:
            raise ValueError(
                f"registry uses schema version {v}, this vahub understands "
                f"{REGISTRY_SCHEMA_VERSION}; upgrade vahub"
            )
        return v


def parse_source_spec(spec: str) -> Source:
    """Parse the argument of `vahub module add --source`.

    git+https://host/repo.git@v1.2.3#subdir=modules/foo
    pypi:vahub-mod-foo==1.0.0
    ./local/path
    """
    text = spec.strip()
    if text.startswith("git+"):
        rest = text[4:]
        subdir = None
        if "#" in rest:
            rest, fragment = rest.split("#", 1)
            for part in fragment.split("&"):
                if part.startswith("subdir="):
                    subdir = part[len("subdir=") :]
        if "@" not in rest.rsplit("/", 1)[-1]:
            raise ValueError("a git source must be pinned: git+https://host/repo.git@<tag-or-sha>")
        url, rev = rest.rsplit("@", 1)
        return GitSource(url=url, rev=rev, subdir=subdir)
    if text.startswith("pypi:"):
        pkg = text[5:]
        if "==" not in pkg:
            raise ValueError("a pypi source must be pinned: pypi:name==version")
        name, version = pkg.split("==", 1)
        return PypiSource(package=name, version=version)
    return PathSource(path=text)
