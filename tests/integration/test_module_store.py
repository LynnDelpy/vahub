"""ModuleStore: the on-disk record of what is installed, and taking it away."""

from __future__ import annotations

from pathlib import Path

import pytest

from vahub.contracts.manifest import Manifest
from vahub.contracts.registry import PathSource
from vahub.modules.store import InstallRecord, ModuleStore, StoreError

pytestmark = pytest.mark.integration


def _store(tmp_path: Path) -> ModuleStore:
    state = tmp_path / "state"
    mods = tmp_path / "modules.d"
    state.mkdir()
    mods.mkdir()
    return ModuleStore(state_dir=state, modules_dir=mods)


def _install(store: ModuleStore, name: str = "demo", *, has_venv: bool = False) -> None:
    """Materialise the two on-disk parts of an installed module: the record under
    the state dir and the manifest under the modules dir."""
    store.module_dir(name).mkdir(parents=True, exist_ok=True)
    store.write_record(
        InstallRecord(
            name=name,
            version="0.1.0",
            source=PathSource(path="/some/where"),
            installed_at=123.0,
            has_venv=has_venv,
        )
    )
    manifest = Manifest.model_validate({"name": name, "runtime": {"command": ["true"]}})
    store.manifest_path(name).write_text(manifest.to_yaml())


def test_record_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.is_installed("demo") is False
    _install(store, "demo")
    assert store.is_installed("demo") is True

    record = store.read_record("demo")
    assert record is not None and record.name == "demo" and record.version == "0.1.0"
    assert store.read_record("absent") is None


def test_get_and_list(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _install(store, "alpha")
    _install(store, "beta")

    module = store.get("alpha")
    assert module is not None and module.name == "alpha"
    assert module.manifest is not None and module.complete is True

    names = [m.name for m in store.list_installed()]
    assert names == ["alpha", "beta"]  # sorted


def test_missing_config_uses_the_scoped_or_bare_env(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    store.module_dir("ha").mkdir(parents=True)
    store.manifest_path("ha").write_text(
        Manifest.model_validate(
            {"name": "ha", "runtime": {"command": ["true"]}, "config": {"required": ["HA_TOKEN"]}}
        ).to_yaml()
    )
    store.write_record(
        InstallRecord(
            name="ha", version="0.1.0", source=PathSource(path="/x"), installed_at=1.0, has_venv=False
        )
    )
    module = store.get("ha")
    assert module is not None
    assert module.missing_config({}) == ["HA_TOKEN"]
    assert module.missing_config({"HA_TOKEN": "t"}) == []  # bare form
    assert module.missing_config({"VAHUB_MOD_HA_HA_TOKEN": "t"}) == []  # scoped form


def test_remove(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _install(store, "demo")
    removed = store.remove("demo")
    assert removed  # returned the paths it deleted
    assert store.is_installed("demo") is False
    assert not store.manifest_path("demo").exists()


def test_remove_is_idempotent(tmp_path: Path) -> None:
    # Removing an absent (or half-installed) module finishes the job rather than
    # failing.
    store = _store(tmp_path)
    assert store.remove("nope") == []


@pytest.mark.parametrize("bad", ["../etc", "Bad Name", "a/b", "", ".", "x" * 100])
def test_a_bad_module_name_is_rejected(tmp_path: Path, bad: str) -> None:
    store = _store(tmp_path)
    with pytest.raises(StoreError):
        store.module_dir(bad)


def test_as_dict_reports_missing_manifest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # A record with no manifest file is incomplete, not a crash.
    store.module_dir("demo").mkdir(parents=True)
    store.write_record(
        InstallRecord(
            name="demo", version="0.1.0", source=PathSource(path="/x"), installed_at=1.0, has_venv=False
        )
    )
    module = store.get("demo")
    assert module is not None and module.complete is False
    data = module.as_dict()
    assert data["name"] == "demo" and data["complete"] is False
