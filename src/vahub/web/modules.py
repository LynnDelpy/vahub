"""Module management from the web UI: install, configure, remove.

An **admin** can do from the browser what `vahub module` does from the shell:
browse the registry, install a module, set the tokens it needs, and remove it,
all without a restart. These routes are guarded by the login, restricted to the
admin role, and origin-checked on every write.

Why the admin role and not merely a signed-in account: installing a module runs
somebody else's code on the host, and configuring one hands it a credential.
Those are the two most consequential things the web interface can do, so they
belong to whoever runs the hub rather than to everyone who may talk to the
assistant. A plain user still *sees* the installed apps (the dashboard and the
automation builder are built out of them), but the view they get carries no
configuration and no operator detail, and every write here refuses them.

Three boundaries hold here, deliberately.

Installing a module grants the assistant nothing. Its tools stay denied to the
model and the scheduler until a policy rule is written in vahub.yaml, which is a
file-and-CLI action the UI cannot perform. So the assistant can never install
itself a capability. (A dashboard card can read a module's own read-declared
tools through the owner endpoint without a rule; see moduleapi.call_read for what
that trusts and what it does not.)

The configuration values set here (often API tokens) are stored in the database,
scoped to one module, and never read back to the browser: the UI learns which
keys are set, not what they are. The supervisor reads them when it builds the
module's environment, exactly as it reads a scoped environment variable.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..modules.installer import Installer, InstallError
from ..modules.registry_client import RegistryClient, RegistryError
from ..modules.store import ModuleStore, StoreError
from ..modules.verify import VerifyError
from . import auth as web_auth
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
        """The installed apps. Everyone signed in may read this, because the
        dashboard and the automation builder are made of it, but only an admin
        gets the operator half: which configuration keys are set, why a module
        failed to start, and whether the policy has a rule for it. To a plain
        user an app is a name, a state and what it can do."""
        admin = await web_auth.is_admin(request, rt)
        store = ModuleStore.from_config(rt.config)
        # Reading and parsing every manifest touches the disk, so it runs off the
        # event loop rather than stalling other requests.
        installed = await asyncio.to_thread(store.list_installed)
        out = []
        for m in installed:
            manifest = m.manifest
            live = rt.supervisor.modules.get(m.name)
            # The live tool list carries each tool's description and input schema.
            # The UI needs both to offer real form fields (a "station" box) instead
            # of asking a person to type JSON, so they are passed through here.
            live_tools = {
                t.get("name"): t for t in (live.tools if live is not None else []) if isinstance(t, dict)
            }
            tools = (
                [
                    {
                        "name": t,
                        "class": spec.cls,
                        "description": _tool_text(live_tools.get(t), spec.description),
                        "schema": _tool_schema(live_tools.get(t)),
                    }
                    for t, spec in manifest.tools.items()
                ]
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
            entry: dict[str, Any] = {
                "name": m.name,
                "version": m.version,
                "description": manifest.description if manifest is not None else "",
                "state": state,
                "tools": tools,
            }
            if admin:
                entry.update(
                    {
                        "last_error": last_error,
                        "missing_config": missing,
                        "config": {
                            "required": required,
                            "optional": optional,
                            "set": await rt.store.module_config_keys(m.name),
                        },
                        "has_policy_rule": has_rule,
                    }
                )
            out.append(entry)
        return JSONResponse({"modules": out, "can_manage": admin})

    @router.get("/modules/available")
    async def available(request: Request, q: str = "") -> JSONResponse:
        # The catalogue is the first half of installing something, so it is the
        # admin's view; there is nothing here a plain user can act on.
        await web_auth.require_admin(request, rt)
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
        await web_auth.require_admin(request, rt)
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
        await web_auth.require_admin(request, rt)
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
        await web_auth.require_admin(request, rt)
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
        await web_auth.require_admin(request, rt)
        check_origin(request, rt.config)
        # Hold the install lock so a config change cannot interleave with an
        # install or remove of the same module. Without it, a config edit racing
        # a remove could re-read the manifest mid-deletion and resurrect the
        # module as a ghost pointing at a deleted venv.
        async with install_lock:
            manifest = _manifest(rt, name)
            if manifest is None:
                return JSONResponse({"ok": False, "error": "unknown_module"}, status_code=404)
            declared = set(manifest.config.required) | set(manifest.config.optional)
            if key not in declared:
                # Only keys the module says it reads may be set, so the form cannot
                # be used to inject arbitrary environment into a module.
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
        await web_auth.require_admin(request, rt)
        check_origin(request, rt.config)
        async with install_lock:  # mutually exclusive with install/remove, as above
            # A removed module has no manifest on disk; refuse to touch config for
            # one, so a delete racing a remove cannot resurrect it.
            if _manifest(rt, name) is None:
                return JSONResponse({"ok": False, "error": "unknown_module"}, status_code=404)
            removed = await rt.store.delete_module_config(name, key)
            rt.supervisor.update_module_config(name, await rt.store.module_config(name))
            await rt.supervisor.apply_config(name)
            state, missing = _live_state(name)
        return JSONResponse(
            {"ok": removed, "name": name, "key": key, "state": state, "missing_config": missing}
        )

    return router


def _tool_text(live: Any, declared: str | None) -> str | None:
    """A tool's description: what the running module says, or the manifest's."""
    if isinstance(live, dict) and isinstance(live.get("description"), str):
        return live["description"]
    return declared


def _tool_schema(live: Any) -> dict[str, Any] | None:
    """A tool's JSON input schema, when the running module published a usable one.
    It is produced by code the hub did not write, so anything but an object is
    dropped rather than passed to the browser as if it were a schema."""
    if not isinstance(live, dict):
        return None
    schema = live.get("inputSchema")
    return schema if isinstance(schema, dict) else None


def _manifest(rt: Runtime, name: str) -> Manifest | None:
    module = ModuleStore.from_config(rt.config).get(name)
    return module.manifest if module is not None else None


__all__ = ["build_router"]
