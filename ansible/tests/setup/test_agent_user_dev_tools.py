#!/usr/bin/env python3
"""The claude agent user gets the commit hooks and the Ansible collections the operator has.

Without prek the agent's commits skipped gitleaks and the commit-time ratchets (#4100). Without
a readable collections fallback every playbook it ran from a worktree died at `couldn't resolve
module/action` (#4108). Without jsonq the operator's instruction to use it failed (#4101). Each
test pins one leg of the fix, with a reject case beside it.

Run: uv run pytest ansible/tests/setup/test_agent_user_dev_tools.py
"""

import tomllib
from pathlib import Path

from _agent_package import agent_setup_script
from _helpers import REPO
from lib.proc_testing import run
from _setup_render import CLAUDE_CODE, render_component_text

CLONE_SYNC = CLAUDE_CODE / "files" / "claude-clone-sync.sh"


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


def collections_path_problems(profile: str, clone: str) -> list[str]:
    """Where the profile's collections path misses the local-first, clone-fallback order."""
    lines = [
        ln
        for ln in profile.splitlines()
        if ln.startswith("export ANSIBLE_COLLECTIONS_PATH=")
    ]
    if len(lines) != 1:
        return [f"{len(lines)} ANSIBLE_COLLECTIONS_PATH exports"]
    entries = lines[0].split("=", 1)[1].split(":")
    want = ["ansible/collections", f"{clone}/ansible/collections"]
    return [] if entries == want else [f"entries {entries}"]


def test_the_profile_falls_back_to_the_agents_own_collections() -> None:
    clone = "/srv/clone"
    profile = render_component_text(
        "claude-code",
        "agent-user-profile.j2",
        {
            "claude_code_agent_user_home": "/srv/agent",
            "claude_code_agent_user_clone_dir": clone,
        },
    )
    assert collections_path_problems(profile, clone) == []
    # The fallback is only worth something where the clone sync installs into it.
    assert (
        "ansible-galaxy collection install -r ansible/requirements.yml -p ansible/collections"
        in CLONE_SYNC.read_text()
    )


def test_an_operator_fallback_or_a_clone_first_path_is_flagged() -> None:
    clone = "/srv/clone"
    operator = "export ANSIBLE_COLLECTIONS_PATH=ansible/collections:/home/ubuntu/server/ansible/collections\n"
    clone_first = f"export ANSIBLE_COLLECTIONS_PATH={clone}/ansible/collections:ansible/collections\n"
    assert collections_path_problems(operator, clone) != []
    assert collections_path_problems(clone_first, clone) != []
    assert collections_path_problems("PATH=x\n", clone) == [
        "0 ANSIBLE_COLLECTIONS_PATH exports"
    ]


JSONQ_COPY = "Give the agent user the operator's jsonq"
