"""The Longhorn host tasks carry `longhorn_host` and no tag that names a Flux-owned object (H1.5a).

From H1.5a until H1.7b, `k3s-bringup.yml --skip-tags kubeconfig,longhorn,longhorn_backup` was the
first step of a rebuild, and a `--skip-tags` exclusion matches any tag a task carries. A host task
that also carried `longhorn` or `longhorn_backup` would have been skipped with them, and a rebuilt
node would never have got `open-iscsi`. H1.7b closed that window: no task carries either tag, and
step 1 skips only `kubeconfig`.

`tasks/longhorn-host.yml` still holds three named tasks that carry `longhorn_host` and neither
retired tag, counting the tags their import in `main.yml` adds. A task that regained one of those
tags would again name an object that Flux owns. The plan is
docs/target-architecture/host/h1-cluster-config-to-flux.md, *The Longhorn window, H1.5a until H1.7b*.

Run: uv run pytest ansible/tests/setup/test_k3s_longhorn_host_tasks.py
"""

from lib import yaml_fast
from lib.repo_paths import K3S_ROLE

TASKS = K3S_ROLE / "tasks"
HOST_FILE = "longhorn-host.yml"
RETIRED_TAGS = frozenset({"longhorn", "longhorn_backup"})
# The three tasks the plan moves; the test must find each by name.
MEMBERS = (
    "Install Longhorn prerequisites",
    "Blacklist Longhorn devices from multipathd",
    "Restart multipathd after a blacklist change",
)
_IMPORTS = ("import_tasks", "ansible.builtin.import_tasks")


def _tags(task: dict) -> frozenset[str]:
    own = task.get("tags") or []
    return frozenset([own] if isinstance(own, str) else own)


def _import_of(main: list[dict], filename: str) -> dict | None:
    for task in main:
        if any(task.get(key) == filename for key in _IMPORTS):
            return task
    return None


def problems(host_tasks: list[dict], main: list[dict]) -> list[str]:
    """Each way the host tasks would be skipped by step 1, or go missing."""
    found = []
    imported = _import_of(main, HOST_FILE)
    if imported is None:
        return [f"main.yml does not import {HOST_FILE}"]
    by_name = {t.get("name"): t for t in host_tasks}
    for name in MEMBERS:
        task = by_name.get(name)
        if task is None:
            found.append(f"{HOST_FILE} holds no task named {name!r}")
            continue
        effective = _tags(task) | _tags(imported)
        if "longhorn_host" not in effective:
            found.append(f"{name!r} does not carry longhorn_host")
        if effective & RETIRED_TAGS:
            found.append(
                f"{name!r} carries {sorted(effective & RETIRED_TAGS)}, a retired tag"
            )
    return found


def _real() -> tuple[list[dict], list[dict]]:
    return (
        yaml_fast.safe_load((TASKS / HOST_FILE).read_text()),
        yaml_fast.safe_load((TASKS / "main.yml").read_text()),
    )


def test_the_host_tasks_carry_longhorn_host_and_neither_retired_tag():
    assert problems(*_real()) == []


def test_a_task_tagged_longhorn_is_flagged():
    host, main = _real()
    host[0]["tags"] = ["longhorn_host", "longhorn"]
    assert any("a retired tag" in p for p in problems(host, main))


def test_a_tag_added_by_the_import_is_flagged():
    host, main = _real()
    imported = _import_of(main, HOST_FILE)
    assert imported is not None
    imported["tags"] = ["longhorn_backup"]
    assert any("a retired tag" in p for p in problems(host, main))


def test_a_missing_member_and_a_missing_import_are_flagged():
    host, main = _real()
    assert problems(host[1:], main) == [
        f"{HOST_FILE} holds no task named {MEMBERS[0]!r}"
    ]
    assert problems(host, [t for t in main if not _import_of([t], HOST_FILE)]) == [
        f"main.yml does not import {HOST_FILE}"
    ]
