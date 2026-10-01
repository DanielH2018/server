#!/usr/bin/env python3
"""Every Python module in the renovate_agent role's `files/` reaches the host, and is watched.

The role enumerates its modules by name twice — once in the "Install agent Python files" copy
loop, once in `stamp_deployed_pairs` for manifest-prune-check.sh's stale-script arm. Neither
list is derived from the directory, so a module added to `files/` and to neither list is one
the host never receives, and the suite stays green because every test reaches `files/` through
its own `sys.path` insert.

Run: uv run pytest ansible/tests/setup/test_renovate_agent_modules_are_shipped.py
"""

from lib import yaml_fast
from _helpers import ANSIBLE

ROLE = ANSIBLE / "roles" / "setup" / "renovate_agent"
TASKS = ROLE / "tasks" / "main.yml"


def listing_problems(shipped: set[str], tasks: list[dict]) -> list[str]:
    """Which of `shipped` the role's two hand-written module lists leave out."""
    (copy,) = [t for t in tasks if t.get("name") == "Install agent Python files"]
    copied = {item for item in copy["loop"] if item.endswith(".py")}
    (stamp,) = [
        t for t in tasks if t.get("name") == "Record the deployed renovate-agent code"
    ]
    stamped = {
        pair["src"].rsplit("/", 1)[1] for pair in stamp["vars"]["stamp_deployed_pairs"]
    }
    problems = []
    if shipped - copied:
        problems.append(
            f"not installed on the host: {sorted(shipped - copied)} — add them to the "
            "'Install agent Python files' loop"
        )
    if shipped - stamped:
        problems.append(
            f"not stamped for drift detection: {sorted(shipped - stamped)} — add them "
            "to stamp_deployed_pairs"
        )
    return problems


def test_every_shipped_module_is_installed_and_stamped() -> None:
    shipped = {p.name for p in (ROLE / "files").glob("*.py")}
    # The census finds its subject by glob, so it names members it must find: the entry
    # module and the two modules it splits into.
    for member in ("renovate_agent.py", "agent_toolbox.py", "run_worktree.py"):
        assert member in shipped, f"{member} is gone from the role's files/"
    assert listing_problems(shipped, yaml_fast.safe_load(TASKS.read_text())) == []


def test_a_module_missing_from_either_list_is_flagged() -> None:
    """The rejecting half: a module neither list names must produce both complaints."""
    tasks = yaml_fast.safe_load(TASKS.read_text())
    problems = listing_problems({"newly_split.py"}, tasks)
    assert len(problems) == 2
    assert all("newly_split.py" in p for p in problems)
