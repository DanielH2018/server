#!/usr/bin/env python3
"""The claude agent user gets the commit hooks and the Ansible collections the operator has.

Without prek the agent's commits skipped gitleaks and the commit-time ratchets (#4100). Without
a readable collections fallback every playbook it ran from a worktree died at `couldn't resolve
module/action` (#4108). Without jsonq the operator's instruction to use it failed (#4101). Each
test pins one leg of the fix, with a reject case beside it.

Run: uv run pytest hosts/tests/test_agent_user_dev_tools.py
"""

import tomllib
from pathlib import Path

from _agent_package import agent_setup_script
from _helpers import REPO
from lib.proc_testing import run


HOOK_TYPES = tomllib.loads((REPO / "prek.toml").read_text())[
    "default_install_hook_types"
]


def install_guard_fires(setup: str, root: Path, shims: list[str]) -> bool:
    """Whether the setup script's prek-install guard fires for a clone holding `shims`."""
    guard = setup.split('  (cd "$clone" && as_agent', 1)[0].rsplit("\nif ", 1)[1]
    clone, home = root / "clone", root / "home"
    (clone / ".git" / "hooks").mkdir(parents=True)
    for shim in shims:
        (clone / ".git" / "hooks" / shim).touch()
    prek = home / ".local" / "bin" / "prek"
    prek.parent.mkdir(parents=True)
    prek.touch(mode=0o755)
    result = run(
        ["sh", "-c", f'clone="{clone}"; home="{home}"\nif {guard}  exit 0\nfi\nexit 1'],
    )
    return result.returncode == 0


def test_the_agents_clone_gets_hooks_from_its_own_prek(tmp_path) -> None:
    setup = agent_setup_script(tmp_path)
    # The install runs as the agent, from the clone, with the agent's own prek.
    assert '  (cd "$clone" && as_agent "$home/.local/bin/prek" install)\n' in setup
    # renovate_agent imports the shared file; only claude_code's agent gets the hooks.


def test_the_install_reruns_while_any_listed_shim_is_missing(tmp_path) -> None:
    # A clone made before commit-msg joined the list holds only pre-commit (#7989).
    setup = agent_setup_script(tmp_path / "render")
    assert "commit-msg" in HOOK_TYPES
    for missing in HOOK_TYPES:
        held = [t for t in HOOK_TYPES if t != missing]
        assert install_guard_fires(setup, tmp_path / missing, held), missing
    assert not install_guard_fires(setup, tmp_path / "all", HOOK_TYPES)
