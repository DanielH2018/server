"""Every task the deploy playbooks run can be reached by some `--tags` value (issue #2704).

WHY. A task no tag selects runs only on a full, untagged `ansible-playbook` run. A change to it
then costs a ~20-minute deploy of the whole fleet, or waits undeployed until the next one.

THE MODEL, measured on ansible-core against a probe playbook on 2026-09-26:

  - A task is selected by its own `tags:` or by tags it inherits.
  - A `roles:` entry, a `block:`, an `import_tasks` and an `import_role` pass their tags down.
  - An `include_tasks` or `include_role` passes down ONLY what its `apply: tags:` names. Its
    own `tags: always` selects the include statement and nothing inside it: an untagged task
    in the included file ran on the untagged run and on no `--tags` run.
  - Tags a task has inherited reach every include below it, `apply` or not. A shared role
    included from a caller's `apply`-tagged role ran its untagged tasks under the caller's tag.

So the walk only descends where nothing is inherited: into an `include_*` without `apply`,
whose target must then tag every task itself. `k8s/rollout-drain` is the live instance, and
its CLAUDE.md records why its `tags: [always]` are load-bearing.

A shared role inside a workload role is covered by this model, but its tag is the caller's:
`deploy.sh --tags <shared role>` selects it by expanding to its callers
(`scripts/deploy_tools/tests/test_deploy_expands_shared_roles.py`).

Run: uv run pytest ansible/tests/deploy/test_every_playbook_task_is_tag_selectable.py
"""

from pathlib import Path

from lib import yaml_fast
from _helpers import ANSIBLE

# The playbooks an operator applies. `k3s-bringup.yml` is in scope with the two the issue names:
# a task it cannot select is the same full-run cost, paid on the control plane.
PLAYBOOKS = ("deploy.yml", "initial_setup.yml", "k3s-bringup.yml")
# The roles_path entries of ansible/ansible.cfg, in its first-match order.
ROLES_PATH = ("roles/containers", "roles/setup", "roles")
_BLOCK_KEYS = ("block", "rescue", "always")
_IMPORTS = {"import_tasks", "import_role"}
_INCLUDES = {"include_tasks", "include_role"}


def _action(task: dict) -> tuple[str, object] | None:
    """The task's include or import keyword, short-named, with its argument."""
    for key, value in task.items():
        short = key.removeprefix("ansible.builtin.")
        if short in _IMPORTS | _INCLUDES:
            return short, value
    return None


def _has_tags(node: dict) -> bool:
    tags = node.get("tags")
    return bool(tags) if not isinstance(tags, str) else bool(tags.strip())


def _role_tasks(ansible: Path, arg) -> Path | None:
    """The tasks file an `*_role` call runs, or None when its name is templated."""
    name = arg.get("name", "") if isinstance(arg, dict) else str(arg)
    tasks_from = arg.get("tasks_from", "main") if isinstance(arg, dict) else "main"
    if "{{" in name:
        return None
    for root in ROLES_PATH:
        role = ansible / root / name
        if role.is_dir():
            return role / "tasks" / f"{Path(tasks_from).stem}.yml"
    return None


def _tasks_file(ansible: Path, here: Path, arg) -> Path | None:
    """The file an `*_tasks` call runs, relative to the including file's directory."""
    path = arg.get("file", "") if isinstance(arg, dict) else str(arg)
    if "{{" in path:
        return None
    return (here / path).resolve()


def unselectable(ansible: Path, playbooks=PLAYBOOKS) -> tuple[list[str], set[str]]:
    """Every task no `--tags` value selects, and the files the walk descended into.

    Returns `(findings, visited)`: a finding is `<file>: <task name>`, and `visited` holds each
    file the walk read with nothing inherited, relative to `ansible`.
    """
    findings: list[str] = []
    visited: set[str] = set()

    def walk(tasks, inherited: bool, source: Path) -> None:
        for task in tasks or []:
            if not isinstance(task, dict):
                continue
            label = f"{source.relative_to(ansible)}: {task.get('name', '<unnamed>')}"
            selected = inherited or _has_tags(task)
            if not selected:
                findings.append(label)
                continue
            for key in _BLOCK_KEYS:
                if key in task:
                    walk(task[key], selected, source)
            action = _action(task)
            if action is None:
                continue
            kind, arg = action
            down = selected if kind in _IMPORTS else inherited
            if kind == "include_role" or kind == "include_tasks":
                apply = arg.get("apply", {}) if isinstance(arg, dict) else {}
                down = down or _has_tags(apply or {})
            if down:
                continue
            if kind.endswith("_role"):
                target = _role_tasks(ansible, arg)
            else:
                target = _tasks_file(ansible, source.parent, arg)
            if target is None or not target.is_file():
                findings.append(f"{label} (include target unresolvable, so unaudited)")
                continue
            visited.add(str(target.relative_to(ansible)))
            walk(yaml_fast.safe_load(target.read_text()), False, target)

    for name in playbooks:
        playbook = ansible / name
        for play in yaml_fast.safe_load(playbook.read_text()) or []:
            if "import_playbook" in play:
                continue
            for section in ("pre_tasks", "tasks", "post_tasks"):
                walk(play.get(section), _has_tags(play), playbook)
            for role in play.get("roles") or []:
                entry = role if isinstance(role, dict) else {"role": role}
                if not (_has_tags(play) or _has_tags(entry)):
                    findings.append(f"{name}: role {entry.get('role')}")
    return findings, visited


# The files the walk must descend into on the real tree. Its result is expected to be empty,
# and an empty result is also what a walker that stopped descending returns.
MUST_VISIT = frozenset(
    {
        "tasks/k8s_batch.yml",
        "post_tasks/k8s_stabilise_gate.yml",
        "post_tasks/k8s_image_drift_gate.yml",
        "roles/k8s/rollout-drain/tasks/main.yml",
    }
)


def test_every_task_the_deploy_playbooks_run_is_tag_selectable():
    findings, visited = unselectable(ANSIBLE)
    assert MUST_VISIT <= visited, f"the walk no longer reaches {MUST_VISIT - visited}"
    assert findings == [], (
        "these run only on a full untagged run, so a change to one needs the whole fleet "
        "deployed. Give each a tag (`always` if it must run on every tagged run too), or "
        "include it with `apply: {tags: [...]}`:\n  " + "\n  ".join(findings)
    )


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


_PLAYBOOK = """
- hosts: all
  tasks:
    - name: Run the drain
      ansible.builtin.include_tasks: tasks/drain.yml
      tags: always
    - name: Run a service
      ansible.builtin.include_role:
        name: svc
        apply: {tags: [svc]}
      tags: always
"""
_SVC = "- name: Svc work\n  ansible.builtin.debug: {msg: x}\n"


def test_a_tagged_task_inside_an_always_include_is_clean(tmp_path):
    _write(tmp_path, "pb.yml", _PLAYBOOK)
    _write(tmp_path, "roles/svc/tasks/main.yml", _SVC)
    _write(
        tmp_path,
        "tasks/drain.yml",
        _SVC.replace("{msg: x}\n", "{msg: x}\n  tags: always\n"),
    )
    assert unselectable(tmp_path, ("pb.yml",)) == ([], {"tasks/drain.yml"})


def test_an_untagged_task_inside_an_always_include_is_flagged(tmp_path):
    """The probe's own shape: `tags: always` on the include does not reach this task."""
    _write(tmp_path, "pb.yml", _PLAYBOOK)
    _write(tmp_path, "roles/svc/tasks/main.yml", _SVC)
    _write(tmp_path, "tasks/drain.yml", _SVC)
    assert unselectable(tmp_path, ("pb.yml",))[0] == ["tasks/drain.yml: Svc work"]


def test_an_untagged_play_task_is_flagged(tmp_path):
    _write(
        tmp_path,
        "pb.yml",
        "- hosts: all\n  tasks:\n" + "    " + _SVC.replace("\n  ", "\n      "),
    )
    assert unselectable(tmp_path, ("pb.yml",))[0] == ["pb.yml: Svc work"]
