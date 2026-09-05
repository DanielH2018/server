#!/usr/bin/env python3
"""Every k8s role whose shipped scripts `import stats_lib` must ship it two ways, not one.

`roles/k8s/game-stats-lib/files/stats_lib.py` is the skeleton valheim-stats and
terraria-stats share (Loki fetch, cursor handling, metric rendering, the HTTP handler, the
run loop, the env reader — see that module's docstring). Unlike `host_lib.py`
(`test_host_lib_sibling_copies.py`, the model for this file), a consumer here does not just
need the file ON THE NODE: it ships into a ConfigMap that a python:3.14-alpine pod mounts,
so a role can stage `stats_lib.py` at `roles/k8s/game-stats-lib/tasks/stage.yml` (the
sibling-copy half) and still ship a pod that dies at `import stats_lib` with
ModuleNotFoundError on its next roll, if the role's own `kubectl create configmap
--from-file` never names it. Both halves are checked here.

Run: uv run pytest ansible/tests/k8s/test_game_stats_lib_sibling_copies.py
"""

import ast
from pathlib import Path

from _helpers import K8S_ROLES, load_tasks, walk_tasks

MODULE = "stats_lib"
SHARED_TASK = "game-stats-lib/tasks/stage.yml"
STATS_LIB_SRC = "ansible/roles/k8s/game-stats-lib/files/stats_lib.py"

# The census's non-vacuity assertion — see the repo-root CLAUDE.md on why an `all()` over an
# empty set from a moved/renamed file is a false green. Compared with `==`, not `<=`, so a new
# consumer that forgets either ship step is exactly what this exists to catch.
EXPECTED_CONSUMERS = frozenset({"terraria-stats", "valheim-stats"})

# roles/k8s/game-stats-lib OWNS stats_lib.py; it does not import it as a sibling.
OWNER_ROLE = "game-stats-lib"


def _imports_stats_lib(source: str) -> bool:
    """True when the module imports stats_lib, in either spelling."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == MODULE for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module == MODULE:
                return True
    return False


def _role_dirs(roles_root: Path) -> list[Path]:
    """Every role directory directly under the root (one level, unlike host_lib's two-plane
    walk — this census is scoped to the k8s plane alone, so K8S_ROLES already IS the plane
    root)."""
    return sorted(d for d in roles_root.iterdir() if d.is_dir())


def consumers(roles_root: Path) -> set[str]:
    """Role names whose shipped `files/` scripts import stats_lib."""
    found = set()
    for role in _role_dirs(roles_root):
        if role.name == OWNER_ROLE:
            continue
        files = role / "files"
        if not files.is_dir():
            continue
        for module in files.rglob("*.py"):
            if _imports_stats_lib(module.read_text()):
                found.add(role.name)
                break
    return found


def includes_of(role: Path) -> list[dict]:
    """Every `import_tasks` of the shared stage file in this role, with its vars."""
    out = []
    for task_file in (
        sorted((role / "tasks").rglob("*.yml")) if (role / "tasks").is_dir() else []
    ):
        for task in walk_tasks(load_tasks(task_file)):
            target = task.get("ansible.builtin.import_tasks") or task.get(
                "import_tasks"
            )
            if isinstance(target, str) and target.endswith(SHARED_TASK):
                out.append(task.get("vars") or {})
    return out


def configmap_shell_cmds(role: Path) -> list[str]:
    """Every `create configmap ... --from-file` shell command this role's tasks render."""
    out = []
    for task_file in (
        sorted((role / "tasks").rglob("*.yml")) if (role / "tasks").is_dir() else []
    ):
        for task in walk_tasks(load_tasks(task_file)):
            shell = task.get("ansible.builtin.shell") or task.get("shell")
            if not isinstance(shell, dict):
                continue
            cmd = shell.get("cmd", "")
            if "create configmap" in cmd:
                out.append(cmd)
    return out


def _role(name: str) -> Path:
    matches = [d for d in _role_dirs(K8S_ROLES) if d.name == name]
    assert len(matches) == 1, (
        f"expected one role directory named {name!r}, found {matches}"
    )
    return matches[0]


# --- The census ------------------------------------------------------------------------------


def test_the_census_finds_exactly_the_known_consumers():
    assert consumers(K8S_ROLES) == set(EXPECTED_CONSUMERS)


def test_the_shared_task_file_exists_where_every_caller_names_it():
    assert (K8S_ROLES / "game-stats-lib" / "tasks" / "stage.yml").is_file()


def test_the_owner_files_stats_lib_module_exists():
    assert (K8S_ROLES / "game-stats-lib" / "files" / "stats_lib.py").is_file()


def test_a_synthetic_role_that_imports_stats_lib_without_the_include_is_flagged(
    tmp_path,
):
    """The red proof, built in tmp_path rather than in the tree — a real role directory
    would be picked up by ansible-lint and by test_no_role_ships_a_test_file.py."""
    role = tmp_path / "synthetic"
    (role / "files").mkdir(parents=True)
    (role / "tasks").mkdir()
    (role / "files" / "reader.py").write_text("from stats_lib import env\n")
    (role / "tasks" / "main.yml").write_text(
        "---\n"
        "- name: Stage the reader\n"
        "  ansible.builtin.copy:\n"
        "    src: reader.py\n"
        "    dest: /etc/rancher/k3s/synthetic/reader.py\n"
        "    mode: '0644'\n"
    )
    assert consumers(tmp_path) == {"synthetic"}, (
        "the census stopped seeing a real consumer"
    )
    assert not includes_of(role), (
        "the include detector fired on a role that has no include"
    )

    # And the accepting half of the same pair, so a detector that flagged everything fails too.
    (role / "tasks" / "main.yml").write_text(
        "---\n"
        "- name: Install the shared stats_lib module\n"
        '  ansible.builtin.import_tasks: "{{ role_path }}/../game-stats-lib/tasks/stage.yml"\n'
        "  vars:\n"
        "    game_stats_lib_dest_dir: /etc/rancher/k3s/synthetic\n"
    )
    assert [v["game_stats_lib_dest_dir"] for v in includes_of(role)] == [
        "/etc/rancher/k3s/synthetic"
    ]


# --- What each include declares --------------------------------------------------------------


def test_every_consumer_includes_the_shared_stage_task():
    """The accepting half: the tree as it stands adopts the shared task everywhere."""
    missing = sorted(
        name for name in EXPECTED_CONSUMERS if not includes_of(_role(name))
    )
    assert not missing, (
        f"{missing} import stats_lib but no longer include {SHARED_TASK}, so the sibling "
        f"copy is gone and the script dies at import on its next run."
    )


def test_every_include_names_a_destination_directory():
    for name in sorted(EXPECTED_CONSUMERS):
        for declared in includes_of(_role(name)):
            assert declared.get("game_stats_lib_dest_dir"), (
                f"{name} includes {SHARED_TASK} without game_stats_lib_dest_dir, so the "
                f"copy would land at /stats_lib.py."
            )


# --- The ConfigMap ship list (the half host_lib's model has no analogue for) ------------------


def test_a_synthetic_configmap_missing_stats_lib_is_flagged(tmp_path):
    """The red proof for the ConfigMap-ships-it-too half: a role that stages stats_lib.py on
    the node but forgets it in the `--from-file` list still ships a pod that dies at
    import — this is the check that would have caught that."""
    role = tmp_path / "synthetic"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "---\n"
        "- name: Render the script ConfigMap manifest\n"
        "  ansible.builtin.shell:\n"
        "    cmd: >-\n"
        "      k3s kubectl create configmap synthetic-script\n"
        "      --from-file=synthetic.py=/etc/rancher/k3s/synthetic/synthetic.py\n"
        "      --dry-run=client -o yaml > /etc/rancher/k3s/synthetic/configmap.yaml\n"
    )
    cmds = configmap_shell_cmds(role)
    assert len(cmds) == 1
    assert "stats_lib.py" not in cmds[0]


def test_every_consumer_ships_stats_lib_py_in_its_own_configmap():
    """The direct check: the ConfigMap ship list, not just the node-local copy, must carry
    stats_lib.py — this is the invariant `install_host_lib.yml`'s consumers get for free
    (their only ship step IS the node copy) and a ConfigMap-based consumer does not."""
    for name in sorted(EXPECTED_CONSUMERS):
        role = _role(name)
        cmds = configmap_shell_cmds(role)
        assert cmds, f"{name}: no `create configmap --from-file` task found"
        assert any("stats_lib.py" in cmd for cmd in cmds), (
            f"{name} imports stats_lib but its ConfigMap ship list "
            f"({cmds}) never names stats_lib.py — the pod dies at import on its next roll."
        )


def hand_copies(roles_root: Path) -> list[str]:
    """Task files that still `copy:` stats_lib.py by hand instead of including the shared
    stage task — the duplication install_host_lib.yml's own equivalent guards against."""
    offenders = []
    for role in _role_dirs(roles_root):
        if not (role / "tasks").is_dir():
            continue
        for task_file in sorted((role / "tasks").rglob("*.yml")):
            if role.name == OWNER_ROLE and task_file.name == "stage.yml":
                continue
            for task in walk_tasks(load_tasks(task_file)):
                copy = task.get("ansible.builtin.copy") or task.get("copy")
                if not isinstance(copy, dict):
                    continue
                haystack = f"{copy.get('src', '')} {copy.get('dest', '')} {task.get('loop', '')}"
                if "stats_lib.py" in haystack:
                    offenders.append(str(task_file.relative_to(roles_root)))
    return sorted(set(offenders))


def test_no_role_still_copies_stats_lib_by_hand_is_clean():
    offenders = hand_copies(K8S_ROLES)
    assert not offenders, (
        f"{offenders} copy stats_lib.py by hand again. Include "
        f"{SHARED_TASK} instead — that is the file that owns the source path and the mode."
    )


def test_a_hand_written_stats_lib_copy_is_flagged(tmp_path):
    """The rejecting half of the pair above."""
    role = tmp_path / "backslider" / "tasks"
    role.mkdir(parents=True)
    (role / "main.yml").write_text(
        "---\n"
        "- name: Install the scripts\n"
        "  ansible.builtin.copy:\n"
        '    src: "{{ item }}"\n'
        '    dest: "/etc/rancher/k3s/backslider/{{ item | basename }}"\n'
        "    mode: '0644'\n"
        "  loop:\n"
        "    - reader.py\n"
        '    - "{{ role_path }}/../game-stats-lib/files/stats_lib.py"\n'
    )
    assert hand_copies(tmp_path) == ["backslider/tasks/main.yml"]

    (role / "main.yml").write_text(
        "---\n"
        "- name: Install the scripts\n"
        "  ansible.builtin.copy:\n"
        '    src: "{{ item }}"\n'
        '    dest: "/etc/rancher/k3s/backslider/{{ item | basename }}"\n'
        "    mode: '0644'\n"
        "  loop:\n"
        "    - reader.py\n"
    )
    assert hand_copies(tmp_path) == []


def test_the_shared_task_file_carries_no_tags():
    """Tags UNION in Ansible, so a tag here would also inherit the caller's — matching
    install_host_lib.yml, release_bin.yml, stamp_render.yml and stamp_deployed.yml."""
    shared = K8S_ROLES / "game-stats-lib" / "tasks" / "stage.yml"
    for task in walk_tasks(load_tasks(shared)):
        assert "tags" not in task, task
