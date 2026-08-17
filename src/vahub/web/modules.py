"""Module management from the web UI: install, configure, remove.

A signed-in owner can do from the browser what `vahub module` does from the
shell: browse the registry, install a module, set the tokens it needs, and remove
it, all without a restart. These routes are guarded by the login and
origin-checked on every write, like the rest of the management surface.

Two boundaries hold here, deliberately.

Installing a module never grants it permission. Its tools stay denied until a
policy rule is written in vahub.yaml, which is a file-and-CLI action; the UI
cannot add one. So a module installed here can do nothing to the world until a
human edits the policy, and the assistant can never install itself a capability.

The configuration values set here (often API tokens) are stored in the database,
scoped to one module, and never read back to the browser: the UI learns which
keys are set, not what they are. The supervisor reads them when it builds the
module's environment, exactly as it reads a scoped environment variable.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..modules.installer import Installer, InstallError
from ..modules.registry_client import RegistryClient, RegistryError
from ..modules.store import ModuleStore, StoreError
from ..modules.verify import VerifyError
from .security import check_origin

if TYPE_CHECKING:
    from ..contracts.manifest import Manifest
    from ..core.runtime import Runtime

# A module name as the manifest defines it; a config key as an environment
# variable name. Both are path parameters, so they are pattern-guarded before
# any handler runs.
_MODULE = r"^[a-z][a-z0-9_-]{0,63}$"
_KEY = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"

# Failures that mean "the install could not be done", as opposed to a bug: these
# become a 400 with the reason, not a 500.
_INSTALL_ERRORS = (InstallError, RegistryError, StoreError, VerifyError)


class InstallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=64)
    source: str | None = Field(default=None, max_length=500)
    version: str | None = Field(default=None, max_length=64)
    force: bool = False


class ConfigValueBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1, max_length=8192)


def build_router(rt: Runtime) -> APIRouter:
    router = APIRouter()
    # One install/remove at a time: two concurrent installs would race on the
    # same staging area and module directory.
    install_lock = asyncio.Lock()

    def _installer() -> Installer:
        return Installer(
            rt.config,
            registry=RegistryClient.from_config(rt.config),
            store=ModuleStore.from_config(rt.config),
        )

    def _live_state(name: str) -> tuple[str, list[str]]:
        mod = rt.supervisor.modules.get(name)
        if mod is None:
            return "stopped", []
        return mod.state.value, list(mod.missing_config)

    # --- listing ----------------------------------------------------------
    @router.get("/modules")
    async def list_modules(request: Request) -> JSONResponse:
        store = ModuleStore.from_config(rt.config)
        out = []
        for m in store.list_installed():
            manifest = m.manifest
            live = rt.supervisor.modules.get(m.name)
            tools = (
                [{"name": t, "class": spec.cls} for t, spec in manifest.tools.items()]
                if manifest is not None
                else []
            )
            required = list(manifest.config.required) if manifest is not None else []
            optional = list(manifest.config.optional) if manifest is not None else []
            has_rule = manifest is not None and any(
                f"{m.name}.{t}" in rt.config.policy.rules for t in manifest.tools
            )
            # The supervisor is the live source of truth for a loaded module (its
            # missing_config already folds in database-stored values); fall back
            # to the on-disk view for one not yet loaded.
            state = live.state.value if live is not None else "stopped"
            missing = list(live.missing_config) if live is not None else m.missing_config()
            last_error = live.last_error if live is not None else m.manifest_error
            out.append(
                {
                    "name": m.name,
                    "version": m.version,
                    "description": manifest.description if manifest is not None else "",
                    "state": state,
                    "last_error": last_error,
                    "missing_config": missing,
                    "tools": tools,
                    "config": {
                        "required": required,
                        "optional": optional,
                        "set": await rt.store.module_config_keys(m.name),
                    },
                    "has_policy_rule": has_rule,
                }
            )
        return JSONResponse({"modules": out})

    @router.get("/modules/available")
    async def available(request: Request, q: str = "") -> JSONResponse:
        store = ModuleStore.from_config(rt.config)
        installed = {m.name for m in store.list_installed()}
        registry = RegistryClient.from_config(rt.config)
        try:
            # The registry fetch is blocking (httpx sync + disk cache), so it runs
            # off the event loop.
            hits = await asyncio.to_thread(registry.search, q[:100])
        except Exception as e:
            # A registry that is unreachable or serving a bad index must degrade
            # to "nothing available", not a 500 that breaks the modules page.
            return JSONResponse({"available": [], "error": str(e), "warnings": registry.warnings})
        items = [
            {
                "name": name,
                "description": entry.description,
                "latest": entry.latest,
                "installed": name in installed,
            }
            for name, entry in hits
        ]
        return JSONResponse({"available": items, "warnings": registry.warnings})

    # --- install / remove -------------------------------------------------
    @router.post("/modules")
    async def install(body: InstallBody, request: Request) -> JSONResponse:
        check_origin(request, rt.config)
        if not body.name and not body.source:
            return JSONResponse({"ok": False, "error": "need_name_or_source"}, status_code=400)
        async with install_lock:
            try:
                result = await asyncio.to_thread(
                    _installer().install,
                    body.name,
                    version=body.version,
                    source_spec=body.source,
                    force=body.force,
                )
            except _INSTALL_ERRORS as e:
                return JSONResponse(
                    {"ok": False, "error": "install_failed", "detail": str(e)}, status_code=400
                )
            name = result.name
            # Bring it into the running hub: load its stored config, then start it
            # if it is fully configured (otherwise it waits, unconfigured, for a
            # token to be set below).
            rt.supervisor.update_module_config(name, await rt.store.module_config(name))
            await rt.supervisor.load_module(name)
        state, missing = _live_state(name)
        return JSONResponse(
            {
                "ok": True,
                "name": name,
                "version": result.version,
                "state": state,
                "missing_config": missing,
                "warnings": list(result.warnings),
            }
        )

    @router.delete("/modules/{name}")
    async def remove(request: Request, name: str = Path(pattern=_MODULE)) -> JSONResponse:
        check_origin(request, rt.config)
        async with install_lock:
            await rt.supervisor.remove_module(name)  # stop the live process first
            try:
                await asyncio.to_thread(_installer().remove, name)
            except _INSTALL_ERRORS as e:
                return JSONResponse(
                    {"ok": False, "error": "remove_failed", "detail": str(e)}, status_code=400
                )
            await rt.store.delete_all_module_config(name)
        return JSONResponse({"ok": True, "name": name})

    # --- per-module configuration -----------------------------------------
    @router.get("/modules/{name}/config")
    async def get_config(request: Request, name: str = Path(pattern=_MODULE)) -> JSONResponse:
        manifest = _manifest(rt, name)
        if manifest is None:
            return JSONResponse({"ok": False, "error": "unknown_module"}, status_code=404)
        return JSONResponse(
            {
                "name": name,
                "required": list(manifest.config.required),
                "optional": list(manifest.config.optional),
                "set": await rt.store.module_config_keys(name),
            }
        )

    @router.put("/modules/{name}/config/{key}")
    async def set_config(
        body: ConfigValueBody,
        request: Request,
        name: str = Path(pattern=_MODULE),
        key: str = Path(pattern=_KEY),
    ) -> JSONResponse:
        check_origin(request, rt.config)
        manifest = _manifest(rt, name)
        if manifest is None:
            return JSONResponse({"ok": False, "error": "unknown_module"}, status_code=404)
        declared = set(manifest.config.required) | set(manifest.config.optional)
        if key not in declared:
            # Only keys the module says it reads may be set, so the form cannot be
            # used to inject arbitrary environment into a module.
            return JSONResponse({"ok": False, "error": "undeclared_key", "detail": key}, status_code=400)
        await rt.store.set_module_config(name, key, body.value)
        rt.supervisor.update_module_config(name, await rt.store.module_config(name))
        await rt.supervisor.apply_config(name)
        state, missing = _live_state(name)
        return JSONResponse({"ok": True, "name": name, "state": state, "missing_config": missing})

    @router.delete("/modules/{name}/config/{key}")
    async def delete_config(
        request: Request,
        name: str = Path(pattern=_MODULE),
        key: str = Path(pattern=_KEY),
    ) -> JSONResponse:
        check_origin(request, rt.config)
        removed = await rt.store.delete_module_config(name, key)
        rt.supervisor.update_module_config(name, await rt.store.module_config(name))
        await rt.supervisor.apply_config(name)
        state, missing = _live_state(name)
        return JSONResponse(
            {"ok": removed, "name": name, "key": key, "state": state, "missing_config": missing}
        )

    return router


def _manifest(rt: Runtime, name: str) -> Manifest | None:
    module = ModuleStore.from_config(rt.config).get(name)
    return module.manifest if module is not None else None


__all__ = ["build_router"]
