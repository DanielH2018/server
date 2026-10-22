"""The stale-checkout guard's map stays honest, and the guard runs right after server.yml (H1.1h).

The map is `k3s_stale_checkout_rows` in each import of `tasks/stale-checkout-guard.yml`. Every row
names a task that exists, every cluster writer outside storage_smoke.yml and agent_verify.yml
has a row, and the import holding a row carries exactly its task's effective tags. Effective
tags are computed as `scripts/deploy_tools/narrow_lib/task_walk.py:_task_tags` computes them,
with block tags and an include's `apply:` tags inherited. The plan is
docs/target-architecture/host/h1-cluster-config-to-flux.md, *Verification*, *Stale-checkout
guard*.

Run: uv run pytest ansible/tests/setup/test_k3s_stale_checkout_map.py
"""

from deploy_tools.narrow_lib.task_walk import _dynamic_includes

from _k3s_cluster_writes import cluster_writer_names
from _role_census import role_task_files
from lib import yaml_fast
from lib.repo_paths import K3S_ROLE

TASKS = K3S_ROLE / "tasks"
GUARD = "stale-checkout-guard.yml"
CHECK = "stale-checkout-check.yml"
# The writers H1 keeps: a throwaway PVC and the joining node's own Longhorn Node CR, neither of
# which a Flux tag declares.
UNMAPPED = frozenset({"storage_smoke.yml", "agent_verify.yml"})
_IMPORTS = ("import_tasks", "ansible.builtin.import_tasks")

TagsByTask = dict[str, set[frozenset[str]]]


def _tags(task: dict) -> frozenset[str]:
    own = task.get("tags") or []
    return frozenset(str(t) for t in ([own] if isinstance(own, str) else own))


def _import_of(task: dict) -> str | None:
    for key in _IMPORTS:
        if key in task:
            value = task[key]
            return value.get("file") if isinstance(value, dict) else value
    return None


def _named_tags(tasks, inherited: frozenset[str], out: TagsByTask) -> None:
    """Each task's name and effective tag set, walked the way `task_walk._task_tags` walks."""
    for task in tasks or []:
        effective = inherited | _tags(task)
        nested = [
            task.get(key) for key in ("block", "rescue", "always") if task.get(key)
        ]
        if nested:
            for inner in nested:
                _named_tags(inner, effective, out)
        else:
            out.setdefault(str(task.get("name")), set()).add(effective)


def task_tags(files: dict[str, list]) -> TagsByTask:
    """Every task's effective tags across `files`, the role's task files keyed by basename.

    A file `main.yml` imports inherits the import's tags. A file included with `apply: {tags}`
    inherits the tags `task_walk._dynamic_includes` names for it.
    """
    inherited = {name: frozenset() for name in files}
    for task in files.get("main.yml", []):
        if (target := _import_of(task)) in inherited:
            inherited[target] = _tags(task)
    for name, tasks in files.items():
        for included, answer in _dynamic_includes(tasks, inherited[name]):
            if included in inherited and isinstance(answer, frozenset):
                inherited[included] = answer
    out: TagsByTask = {}
    for name, tasks in files.items():
        if name not in ("main.yml", GUARD, CHECK):
            _named_tags(tasks, inherited[name], out)
    return out


def _row_name(task: str) -> str:
    """A row's task name as Ansible renders it: `{% raw %}` keeps a templated name literal."""
    return task.replace("{% raw %}", "").replace("{% endraw %}", "")


def map_problems(guard: list, tags: TagsByTask, writers: set[str]) -> list[str]:
    """Every way the guard's map disagrees with the role's tasks, or [] when it is honest."""
    problems = []
    mapped = set()
    seen: set[frozenset[str]] = set()
    for instance in guard:
        instance_tags = _tags(instance)
        rows = (instance.get("vars") or {}).get("k3s_stale_checkout_rows") or []
        label = f"the instance tagged {sorted(instance_tags)}"
        if _import_of(instance) != CHECK:
            problems.append(f"{label} does not import {CHECK}")
        if instance_tags in seen:
            problems.append(f"{label} repeats a tag set another instance holds")
        seen.add(instance_tags)
        if not rows:
            problems.append(f"{label} has no row left: delete it")
        for row in rows:
            name = _row_name(row["task"])
            mapped.add(name)
            if name not in tags:
                problems.append(f"{label} names {name!r}, which no task file holds")
            elif other := sorted(sorted(t) for t in tags[name] if t != instance_tags):
                problems.append(f"{label} holds {name!r}, whose task carries {other}")
    problems += [
        f"writer {name!r} has no row in {GUARD}" for name in sorted(writers - mapped)
    ]
    return problems


def _role_texts() -> dict[str, str]:
    """Each of the role's task files by its path under `tasks/`."""
    return {
        str(path.relative_to(TASKS)): path.read_text()
        for path in role_task_files(K3S_ROLE)
    }


def _role_files() -> dict[str, list]:
    return {
        name: yaml_fast.safe_load(text) or [] for name, text in _role_texts().items()
    }


def _writers(files: dict[str, str]) -> set[str]:
    """Every cluster writer the census flags outside the guard's files and UNMAPPED."""
    return {
        name
        for file, text in files.items()
        if file not in UNMAPPED | {GUARD, CHECK}
        for name in cluster_writer_names(text)
    }


def test_the_real_map_is_honest():
    # fact: ansible/roles/setup/k3s/CLAUDE.md#Notable
    files = _role_files()
    writers = _writers(_role_texts())
    assert "Install Longhorn" in writers and len(writers) >= 30, sorted(writers)
    assert map_problems(files[GUARD], task_tags(files), writers) == []


def test_a_weekly_shard_writer_takes_its_include_apply_tags():
    # The named member of the tag computation: longhorn.yml includes the shard file in a loop
    # with `apply: {tags: [longhorn]}`, and its own tasks carry no tags.
    tags = task_tags(_role_files())
    assert tags["Move volumes whose weekday changed out of shard d{{ shard }}"] == {
        frozenset({"longhorn"})
    }


_FIXTURE_TAGS: TagsByTask = {
    "Apply the widget": {frozenset({"widget"})},
    "Apply the gadget": {frozenset({"gadget", "widget"})},
}


def _instance(tags: list[str], *tasks: str) -> dict:
    return {
        "name": "Guard",
        "ansible.builtin.import_tasks": CHECK,
        "tags": tags,
        "vars": {"k3s_stale_checkout_rows": [{"task": t, "ids": ["x"]} for t in tasks]},
    }


def test_a_fixture_map_matching_its_tasks_is_clean():
    guard = [
        _instance(["widget"], "Apply the widget"),
        _instance(["gadget", "widget"], "Apply the gadget"),
    ]
    assert (
        map_problems(guard, _FIXTURE_TAGS, {"Apply the widget", "Apply the gadget"})
        == []
    )


def test_a_row_naming_a_missing_task_is_flagged():
    guard = [_instance(["widget"], "Apply the widget", "Apply the gizmo")]
    assert map_problems(guard, _FIXTURE_TAGS, {"Apply the widget"}) == [
        "the instance tagged ['widget'] names 'Apply the gizmo', which no task file holds"
    ]


def test_a_writer_with_no_row_is_flagged():
    guard = [_instance(["widget"], "Apply the widget")]
    assert map_problems(
        guard, _FIXTURE_TAGS, {"Apply the widget", "Apply the gadget"}
    ) == ["writer 'Apply the gadget' has no row in stale-checkout-guard.yml"]


def test_a_row_in_an_instance_with_other_tags_is_flagged():
    # The union shape: one instance tagged with every writer's tags.
    guard = [_instance(["gadget", "widget"], "Apply the widget", "Apply the gadget")]
    assert map_problems(
        guard, _FIXTURE_TAGS, {"Apply the widget", "Apply the gadget"}
    ) == [
        "the instance tagged ['gadget', 'widget'] holds 'Apply the widget', "
        "whose task carries [['widget']]"
    ]


# ── Placement: directly after server.yml, before every other import ──────────────────


def placement_problems(main: list, server_text: str) -> list[str]:
    """Why the guard's import is not where a stale writer and a down API both need it."""
    imports = [_import_of(task) for task in main]
    if "server.yml" not in imports:
        return ["main.yml no longer imports server.yml: the placement has no anchor"]
    problems = []
    after = imports.index("server.yml") + 1
    if imports.count(GUARD) != 1 or imports[after : after + 1] != [GUARD]:
        problems.append(
            f"{GUARD} must be imported once, directly after server.yml; main.yml imports "
            f"{imports}"
        )
    problems += [
        f"server.yml task {name!r} writes the cluster ahead of the guard"
        for name in cluster_writer_names(server_text)
    ]
    return problems


_SERVER = "- name: Wait for the node to report Ready\n  ansible.builtin.command: k3s kubectl wait node --all\n"


def _main(*files: str) -> list:
    return [{"name": f, "ansible.builtin.import_tasks": f} for f in files]


def test_the_real_guard_import_sits_directly_after_server_yml():
    main = yaml_fast.safe_load((TASKS / "main.yml").read_text())
    assert placement_problems(main, (TASKS / "server.yml").read_text()) == []


def test_a_guard_moved_to_the_top_of_main_yml_is_flagged():
    # A whole-role run with the API down would fail the read before server.yml reinstalls k3s.
    main = _main(GUARD, "server.yml", "longhorn.yml")
    assert placement_problems(main, _SERVER)


def test_a_server_yml_that_gains_a_writer_is_flagged():
    server = _SERVER + (
        "- name: Apply something\n  ansible.builtin.command: k3s kubectl apply -f /x.yaml\n"
    )
    assert placement_problems(_main("server.yml", GUARD), server) == [
        "server.yml task 'Apply something' writes the cluster ahead of the guard"
    ]
