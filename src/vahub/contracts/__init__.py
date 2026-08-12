"""The two public contracts: the module manifest and the registry index.

These are what third parties build against, so they are versioned and validated
rather than being implicit conventions.
"""

from .manifest import HEALTH_TOOL, Manifest, load_manifests
from .registry import Registry, parse_source_spec

__all__ = ["HEALTH_TOOL", "Manifest", "Registry", "load_manifests", "parse_source_spec"]
