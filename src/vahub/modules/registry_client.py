"""Fetching the index that `vahub module add <name>` resolves a name against.

Two decisions worth knowing:

* The index is cached on disk and the cache is used when the network is not
  there. An installer that only works online is an installer that fails on the
  machine that matters, so a stale index is preferred over no index, and the
  staleness is reported rather than hidden.
* A registry "URL" may equally be a filesystem path. Running an internal index
  is then a matter of pointing at a file, which is also how the tests avoid the
  network entirely.

The index is only an index: it maps a name to a pinned source. Nothing here
executes anything, and nothing here is trusted beyond "it parses as a Registry".
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlparse

import httpx
from pydantic import ValidationError

from vahub.__about__ import __version__
from vahub.contracts.registry import (
    DEFAULT_REGISTRY_URL,
    ModuleEntry,
    ModuleVersion,
    Registry,
)

if TYPE_CHECKING:
    from vahub.config.models import Config

# The Config model has no registry field, so the URL comes from the command line
# or from here.
ENV_REGISTRY_URL = "VAHUB_REGISTRY_URL"

DEFAULT_TIMEOUT_S = 15.0
DEFAULT_TTL_S = 3600.0

# An index is a small JSON document and it is read into memory. Anything this
# large is a mistake or an attack, and it is refused before it is read.
MAX_INDEX_BYTES = 8 * 1024 * 1024


class RegistryError(Exception):
    """Raised with a message meant for a human at a terminal."""


class RegistryClient:
    """Reads a registry index from a URL or a file, with an on-disk cache."""

    def __init__(
        self,
        url: str | None = None,
        *,
        cache_dir: Path | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        ttl_s: float = DEFAULT_TTL_S,
    ) -> None:
        self.url = url or os.environ.get(ENV_REGISTRY_URL) or DEFAULT_REGISTRY_URL
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.timeout_s = timeout_s
        self.ttl_s = ttl_s
        # Where the last successful load came from, and anything the user should
        # know about it (a stale cache, most importantly).
        self.last_source: str | None = None
        self.warnings: list[str] = []
        self._cached: Registry | None = None

    @classmethod
    def from_config(
        cls,
        config: Config,
        url: str | None = None,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        ttl_s: float = DEFAULT_TTL_S,
    ) -> RegistryClient:
        return cls(url, cache_dir=config.hub.state_dir / "cache", timeout_s=timeout_s, ttl_s=ttl_s)

    # --- locations ---------------------------------------------------------
    @property
    def local_path(self) -> Path | None:
        """The index as a file, when the URL names one. `file:` URLs and bare
        paths are both accepted because both are what people actually type."""
        parsed = urlparse(self.url)
        if parsed.scheme == "file":
            return Path(unquote(parsed.path))
        if parsed.scheme in ("http", "https"):
            return None
        return Path(self.url).expanduser()

    @property
    def cache_path(self) -> Path | None:
        if self.cache_dir is None or self.local_path is not None:
            return None
        # Keyed by URL: pointing at a different index must not read the cache of
        # the previous one.
        digest = hashlib.sha256(self.url.encode("utf-8")).hexdigest()[:12]
        return self.cache_dir / f"registry-{digest}.json"

    # --- loading -----------------------------------------------------------
    def load(self, *, refresh: bool = False, offline: bool = False) -> Registry:
        """Return the index, from memory, the file, the cache or the network."""
        if self._cached is not None and not refresh:
            return self._cached
        self.warnings = []

        path = self.local_path
        if path is not None:
            registry = self._parse(self._read_file(path), origin=str(path))
            return self._remember(registry, str(path))

        cache = self.cache_path
        age = _age_s(cache) if cache is not None else None

        if cache is not None and age is not None and not refresh and age <= self.ttl_s:
            cached = self._try_cache(cache)
            if cached is not None:
                return self._remember(cached, "cache")

        if offline:
            if cache is not None and age is not None:
                cached = self._try_cache(cache)
                if cached is not None:
                    self.warnings.append(f"offline: using the index cached {_ago(age)}")
                    return self._remember(cached, "cache (stale)")
            raise RegistryError(
                f"offline, and no cached copy of the registry at {self.url}. "
                "Install the module directly instead, for example "
                "--source git+https://host/repo@v1.2.3"
            )

        try:
            raw = self._fetch()
        except RegistryError as e:
            if cache is not None and age is not None:
                cached = self._try_cache(cache)
                if cached is not None:
                    self.warnings.append(f"{e}; using the index cached {_ago(age)}")
                    return self._remember(cached, "cache (stale)")
            raise RegistryError(
                f"{e}\n"
                f"  point vahub at a local index ({ENV_REGISTRY_URL}=/path/to/registry.json) "
                "or install the module directly with --source"
            ) from e

        registry = self._parse(raw, origin=self.url)
        self._write_cache(raw)  # only a document that validated is worth caching
        return self._remember(registry, self.url)

    def _remember(self, registry: Registry, source: str) -> Registry:
        self._cached = registry
        self.last_source = source
        return registry

    def _try_cache(self, cache: Path) -> Registry | None:
        """A corrupt or outdated cache is a reason to go to the network, not a
        reason to fail."""
        try:
            return self._parse(cache.read_bytes(), origin=str(cache))
        except (RegistryError, OSError):
            return None

    def _read_file(self, path: Path) -> bytes:
        try:
            if path.stat().st_size > MAX_INDEX_BYTES:
                raise RegistryError(f"registry index {path} is larger than {MAX_INDEX_BYTES} bytes")
            return path.read_bytes()
        except OSError as e:
            raise RegistryError(f"cannot read the registry index at {path}: {e}") from e

    def _fetch(self) -> bytes:
        headers = {"User-Agent": f"vahub/{__version__}", "Accept": "application/json"}
        chunks: list[bytes] = []
        size = 0
        try:
            client = httpx.Client(timeout=self.timeout_s, follow_redirects=True, headers=headers)
            with client, client.stream("GET", self.url) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_INDEX_BYTES:
                        raise RegistryError(
                            f"registry index at {self.url} exceeds {MAX_INDEX_BYTES} bytes; "
                            "refusing to read it"
                        )
                    chunks.append(chunk)
        except httpx.HTTPStatusError as e:
            raise RegistryError(
                f"registry at {self.url} returned HTTP {e.response.status_code}"
            ) from e
        except httpx.HTTPError as e:
            raise RegistryError(f"cannot reach the registry at {self.url}: {e}") from e
        return b"".join(chunks)

    def _parse(self, raw: bytes, *, origin: str) -> Registry:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise RegistryError(f"registry index at {origin} is not valid JSON: {e}") from e
        if not isinstance(data, dict):
            raise RegistryError(f"registry index at {origin} must be a JSON object")
        try:
            return Registry.model_validate(data)
        except ValidationError as e:
            lines = [f"registry index at {origin} is not valid:"]
            for detail in e.errors():
                location = ".".join(str(p) for p in detail["loc"]) or "(root)"
                lines.append(f"  {location}: {detail['msg']}")
            raise RegistryError("\n".join(lines)) from e

    def _write_cache(self, raw: bytes) -> None:
        cache = self.cache_path
        if cache is None:
            return
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(".tmp")
            tmp.write_bytes(raw)
            tmp.replace(cache)  # a reader never sees a half-written index
        except OSError as e:
            # A hub that cannot write its cache still works; it just refetches.
            self.warnings.append(f"could not cache the registry index: {e}")

    # --- queries -----------------------------------------------------------
    def entry(self, name: str, *, offline: bool = False) -> ModuleEntry:
        registry = self.load(offline=offline)
        entry = registry.modules.get(name)
        if entry is None:
            close = difflib.get_close_matches(name, list(registry.modules), n=3)
            hint = f"; did you mean {', '.join(close)}?" if close else ""
            raise RegistryError(f"no module named {name!r} in the registry at {self.url}{hint}")
        return entry

    def resolve(
        self, name: str, version: str | None = None, *, offline: bool = False
    ) -> tuple[str, ModuleVersion]:
        """Pick the version to install: the requested one, else the entry's own
        choice of latest."""
        entry = self.entry(name, offline=offline)
        try:
            return entry.resolve(version)
        except KeyError as e:
            raise RegistryError(f"module {name!r}: {e.args[0]}") from e

    def search(self, query: str = "", *, offline: bool = False) -> list[tuple[str, ModuleEntry]]:
        return self.load(offline=offline).search(query)


def _age_s(path: Path | None) -> float | None:
    if path is None:
        return None
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def _ago(age_s: float) -> str:
    if age_s < 90:
        return f"{int(age_s)}s ago"
    if age_s < 5400:
        return f"{int(age_s // 60)}m ago"
    if age_s < 172800:
        return f"{int(age_s // 3600)}h ago"
    return f"{int(age_s // 86400)}d ago"
