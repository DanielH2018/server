#!/usr/bin/env python3
"""Every Python module in the renovate-agent component's `files/` reaches the host.

`component.yml` lists its files one by one, so a module split out of the wrapper and left off the
list leaves the unit dying at import on the host while every test here, which runs from the source
tree, stays green.

Run: uv run pytest ansible/tests/setup/test_renovate_agent_modules_are_shipped.py
"""

from pathlib import Path

from lib import yaml_fast
from _helpers import ANSIBLE

COMPONENT = ANSIBLE.parent / "hosts" / "components" / "renovate-agent"
SPEC = yaml_fast.safe_load((COMPONENT / "component.yml").read_text())


def listing_problems(shipped: set[str], files: list[dict]) -> list[str]:
    """Which of `shipped` the component's file list leaves out of the install directory."""
    listed = {
        Path(entry["dst"]).name
        for entry in files
        if entry["dst"].startswith("/usr/lib/homelab/renovate-agent/")
    }
    missing = sorted(shipped - listed)
    return [f"not installed on the host: {missing}"] if missing else []


def test_every_shipped_module_is_installed() -> None:
    shipped = {p.name for p in (COMPONENT / "files").glob("*.py")}
    # The census finds its subject by glob, so it names members it must find: the entry
    # module and the two modules it splits into.
    for member in ("renovate_agent.py", "agent_toolbox.py", "run_worktree.py"):
        assert member in shipped, f"{member} is gone from the component's files/"
    assert listing_problems(shipped, SPEC["files"]) == []


def test_a_module_missing_from_the_list_is_flagged() -> None:
    """The rejecting half: a module the list does not name must produce the complaint."""
    problems = listing_problems({"newly_split.py"}, SPEC["files"])
    assert problems == ["not installed on the host: ['newly_split.py']"]
