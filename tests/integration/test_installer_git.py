"""Installing from git: the pinning promise, tested against a real repository.

The documentation says a git source must be pinned to a tag or a commit sha, and
that installing from a branch is refused. The parser can only enforce that for
names it recognises as branches; `develop` and `v1.2.3` are the same shape to it.
The remote is the only thing that knows which is which, so these tests build an
actual repository with both, and install from it over file://. No network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vahub.config.models import Config
from vahub.modules.installer import InstallError, Installer

pytestmark = pytest.mark.integration


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


@pytest.fixture
def module_repo(tmp_path: Path) -> Path:
    """A repository holding one tiny module, on a branch named `develop`, with
    the same commit also tagged `v1.0.0`."""
    repo = tmp_path / "repo"
    (repo / "src" / "vahub_mod_tiny").mkdir(parents=True)
    (repo / "module.yaml").write_text(
        "schema_version: 1\n"
        "name: tiny\n"
        "version: 1.0.0\n"
        'runtime:\n  command: ["{venv}/bin/python", "-m", "vahub_mod_tiny"]\n'
        "tools:\n  ping:\n    class: read\n"
    )
    (repo / "pyproject.toml").write_text(
        "[build-system]\nrequires = ['hatchling']\nbuild-backend = 'hatchling.build'\n\n"
        "[project]\nname = 'vahub-mod-tiny'\nversion = '1.0.0'\nrequires-python = '>=3.12'\n\n"
        "[tool.hatch.build.targets.wheel]\npackages = ['src/vahub_mod_tiny']\n"
    )
    (repo / "src" / "vahub_mod_tiny" / "__init__.py").write_text("")
    _git("init", "--quiet", "--initial-branch", "develop", ".", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "--quiet", "-m", "the module", cwd=repo)
    _git("tag", "v1.0.0", cwd=repo)
    return repo


def _installer(state_dir: Path, modules_dir: Path) -> Installer:
    config = Config.model_validate(
        {
            "hub": {"state_dir": str(state_dir), "modules_dir": str(modules_dir)},
            "llm": {"provider": "mock"},
            "policy": {"default": "deny", "rules": {}},
        }
    )
    return Installer(config)


def test_a_branch_is_refused_even_when_it_is_not_called_main(
    module_repo: Path, state_dir: Path, modules_dir: Path
) -> None:
    """`develop` is not one of the names the parser knows to refuse, so this is
    the case that used to install "whatever that branch says today"."""
    installer = _installer(state_dir, modules_dir)
    with pytest.raises(InstallError) as raised:
        installer.install(source_spec=f"git+file://{module_repo}@develop")
    assert "branch" in str(raised.value)
    assert "pin a tag or a commit sha" in str(raised.value)


def test_the_same_commit_installs_fine_by_its_tag(
    module_repo: Path, state_dir: Path, modules_dir: Path
) -> None:
    """The refusal is about the rev being a moving name, not about the code: the
    identical commit, named by its tag, is installed."""
    installer = _installer(state_dir, modules_dir)
    result = installer.install(source_spec=f"git+file://{module_repo}@v1.0.0")
    assert result.name == "tiny"


def test_a_commit_sha_installs_fine(module_repo: Path, state_dir: Path, modules_dir: Path) -> None:
    sha = subprocess.run(
        ["git", "-C", str(module_repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    installer = _installer(state_dir, modules_dir)
    result = installer.install(source_spec=f"git+file://{module_repo}@{sha}")
    assert result.name == "tiny"
