#!/usr/bin/env python3
"""stats_lib.py reaches a consumer's pod through the include, never through a hand-written entry.

`roles/k8s/game-stats-lib/files/stats_lib.py` is the skeleton valheim-stats and terraria-stats
share. Each consumer ships it into a ConfigMap that a python:3.14-alpine pod mounts beside the
entry script, so a consumer needs two things: the file staged on the node, and a `--from-file`
entry naming it in the consumer's own `kubectl create configmap` command. Until #2055 those
were two hand-kept halves — `game-stats-lib/tasks/stage.yml` did the first and every consumer
spelled the second itself — and a role could do one without the other and ship a pod that dies
at `import stats_lib` on its next roll. A guard checked both halves per consumer, and it was
the second copy of `test_host_lib_sibling_copies.py`.

Now the include owns both halves: `stage.yml` copies the file AND sets
`game_stats_lib_from_file` to the `--from-file` argument for that copy, and a consumer
interpolates the fact into its ship list. The lib's ConfigMap key and its staged basename are
spelled once, in the include. What is left to check is the include's contract, not each
consumer's spelling of it:

  - the fact names exactly the file the copy task stages (key == basename, path == dest);
  - every consumer interpolates the fact, and none spells `stats_lib.py` by hand — a
    hand-written entry is the drift this refactor removed, and a consumer that stages the file
    but never interpolates the fact is the "staged but not mounted" pod;
  - the set of roles that `import stats_lib` is the set of roles that include the file. The
    include cannot enforce its own adoption (a consumer that drops it still deploys and dies at
    import), and an include with no importer is a dead copy.

Run: uv run pytest ansible/tests/k8s/test_game_stats_lib_ships_through_the_include.py
"""

import ast
from pathlib import Path

from jinja2 import Environment
from lib.k8s_roles import role_callers

from _helpers import K8S_ROLES, imported_module_ids, leaf_tasks, load_tasks, walk_tasks

MODULE = "stats_lib"
OWNER_ROLE = "game-stats-lib"
SHARED_TASK = "game-stats-lib/tasks/stage.yml"
DEST_VAR = "game_stats_lib_dest_dir"
FACT = "game_stats_lib_from_file"

# The census's non-vacuity assertion — see the repo-root CLAUDE.md on why an `all()` over an
# empty set from a moved/renamed file is a false green. Compared with `==`, not `<=`, so a new
# consumer that forgets the include is exactly what this exists to catch.
EXPECTED_CONSUMERS = frozenset({"terraria-stats", "valheim-stats"})


def _role_dirs(roles_root: Path) -> list[Path]:
    return sorted(d for d in roles_root.iterdir() if d.is_dir())


def _task_files(role: Path) -> list[Path]:
    return sorted((role / "tasks").rglob("*.yml")) if (role / "tasks").is_dir() else []


def importers(roles_root: Path) -> set[str]:
    """Role names whose shipped `files/` scripts import stats_lib. The owner is not a consumer."""
    found = set()
    for role in _role_dirs(roles_root):
        if role.name == OWNER_ROLE or not (role / "files").is_dir():
            continue
        for module in (role / "files").rglob("*.py"):
            try:
                tree = ast.parse(module.read_text())
            except SyntaxError:
                continue
            if imported_module_ids(tree, {MODULE}):
                found.add(role.name)
                break
    return found


def includes_of(role: Path) -> list[dict]:
    """The vars of every `import_tasks` of the shared stage file in this role, nested too."""
    out = []
    for task_file in _task_files(role):
        for task in walk_tasks(load_tasks(task_file)):
            target = task.get("ansible.builtin.import_tasks") or task.get(
                "import_tasks"
            )
            if isinstance(target, str) and target.endswith(SHARED_TASK):
                out.append(task.get("vars") or {})
    return out


def _is_include(task: dict) -> bool:
    target = task.get("ansible.builtin.import_tasks") or task.get("import_tasks")
    return isinstance(target, str) and target.endswith(SHARED_TASK)


def _shell_cmd(task: dict) -> str:
    shell = task.get("ansible.builtin.shell") or task.get("shell")
    return shell.get("cmd", "") if isinstance(shell, dict) else ""


def configmap_cmds(role: Path) -> list[str]:
    """Every `create configmap ... --from-file` shell command this role's tasks render."""
    return [
        cmd
        for task_file in _task_files(role)
        for task in walk_tasks(load_tasks(task_file))
        if "create configmap" in (cmd := _shell_cmd(task))
    ]


def hand_spellings(role: Path) -> list[str]:
    """Task fields that name `stats_lib.py` by hand — a `copy:` of it, or a ship-list entry.

    Reads the task fields, not the file text: the consumers' comments legitimately mention the
    file, and a text scan would flag the explanation of why the fact exists.
    """
    offenders = []
    for task_file in _task_files(role):
        for task in walk_tasks(load_tasks(task_file)):
            copy = task.get("ansible.builtin.copy") or task.get("copy")
            fields = [_shell_cmd(task)]
            if isinstance(copy, dict):
                fields += [
                    str(copy.get("src", "")),
                    str(copy.get("dest", "")),
                    str(task.get("loop", "")),
                ]
            if any(f"{MODULE}.py" in f for f in fields):
                offenders.append(
                    f"{task_file.relative_to(role.parent)}: {task.get('name')}"
                )
    return offenders


def ships_through_the_fact(cmd: str) -> bool:
    """True when a ship list carries the include's fact and no hand spelling of the lib."""
    return "{{ " + FACT + " }}" in cmd and f"{MODULE}.py" not in cmd


def from_file_arg(arg: str) -> tuple[str, str]:
    """`--from-file=<key>=<path>` -> (key, path)."""
    prefix = "--from-file="
    assert arg.startswith(prefix), arg
    key, _, path = arg[len(prefix) :].partition("=")
    return key, path


def fact_matches_copy(fact_value: str, copy_dest: str) -> bool:
    """The contract the include must hold: the fact ships exactly the file the copy stages."""
    key, path = from_file_arg(fact_value)
    return path == copy_dest and key == Path(copy_dest).name


def stage_contract(dest_dir: str = "/etc/rancher/k3s/synthetic") -> tuple[str, str]:
    """(rendered copy dest, rendered fact value) for a caller staging into `dest_dir`."""
    copy_dest = fact_value = None
    for task in walk_tasks(
        load_tasks(K8S_ROLES / "game-stats-lib" / "tasks" / "stage.yml")
    ):
        copy = task.get("ansible.builtin.copy") or task.get("copy")
        if isinstance(copy, dict):
            copy_dest = copy["dest"]
        facts = task.get("ansible.builtin.set_fact") or task.get("set_fact")
        if isinstance(facts, dict) and FACT in facts:
            fact_value = facts[FACT]
    assert copy_dest and fact_value, (
        "stage.yml no longer carries both the copy and the fact"
    )
    env = Environment()
    ctx = {DEST_VAR: dest_dir}
    return env.from_string(copy_dest).render(ctx), env.from_string(fact_value).render(
        ctx
    )


def _role(name: str) -> Path:
    matches = [d for d in _role_dirs(K8S_ROLES) if d.name == name]
    assert len(matches) == 1, (
        f"expected one role directory named {name!r}, found {matches}"
    )
    return matches[0]


# --- The census ------------------------------------------------------------------------------


def test_the_census_finds_exactly_the_known_consumers():
    assert importers(K8S_ROLES) == set(EXPECTED_CONSUMERS)


def test_the_roles_that_import_the_module_are_the_roles_that_include_the_file():
    """Both directions at once: an importer without the include dies at `import stats_lib`;
    an includer that never imports carries a dead copy. `role_callers` reads the same
    `import_tasks` edge land_tags.py routes a stats_lib.py change through, so this also pins
    that the edge still resolves."""
    assert role_callers()[OWNER_ROLE] == importers(K8S_ROLES)


def test_a_synthetic_role_that_imports_stats_lib_without_the_include_is_flagged(
    tmp_path,
):
    """The red proof, in tmp_path: a real role directory would be picked up by ansible-lint."""
    role = tmp_path / "synthetic"
    (role / "files").mkdir(parents=True)
    (role / "tasks").mkdir()
    (role / "files" / "reader.py").write_text("from stats_lib import run\n")
    (role / "tasks" / "main.yml").write_text(
        "---\n- name: Stage the reader\n  ansible.builtin.copy:\n    src: reader.py\n"
        "    dest: /etc/rancher/k3s/synthetic/reader.py\n    mode: '0644'\n"
    )
    assert importers(tmp_path) == {"synthetic"}
    assert not includes_of(role)

    (role / "tasks" / "main.yml").write_text(
        "---\n- name: Install the shared stats_lib module\n"
        '  ansible.builtin.import_tasks: "{{ role_path }}/../game-stats-lib/tasks/stage.yml"\n'
        f"  vars:\n    {DEST_VAR}: /etc/rancher/k3s/synthetic\n"
    )
    assert [v[DEST_VAR] for v in includes_of(role)] == ["/etc/rancher/k3s/synthetic"]


def test_every_include_names_a_destination_directory():
    for name in sorted(EXPECTED_CONSUMERS):
        for declared in includes_of(_role(name)):
            assert declared.get(DEST_VAR), (
                f"{name} includes {SHARED_TASK} without {DEST_VAR}, so the copy would land "
                f"at /stats_lib.py."
            )


# --- The include's contract: the fact ships the file the copy stages ------------------------


def test_the_fact_names_exactly_the_file_the_include_stages():
    copy_dest, fact_value = stage_contract()
    assert fact_matches_copy(fact_value, copy_dest), (copy_dest, fact_value)
    assert from_file_arg(fact_value) == (
        f"{MODULE}.py",
        f"/etc/rancher/k3s/synthetic/{MODULE}.py",
    )


def test_a_fact_whose_key_or_path_drifts_from_the_copy_is_flagged():
    """The rejecting half: a key the pod's `import stats_lib` cannot resolve, or a path the copy
    never wrote, both fail at pod import rather than at deploy — this is the check that sees
    them."""
    dest = "/etc/rancher/k3s/synthetic/stats_lib.py"
    assert fact_matches_copy(f"--from-file=stats_lib.py={dest}", dest)
    assert not fact_matches_copy(f"--from-file=statslib.py={dest}", dest)
    assert not fact_matches_copy(
        "--from-file=stats_lib.py=/etc/rancher/k3s/other/stats_lib.py", dest
    )


# --- The consumers: interpolate the fact, never spell the lib ---------------------------------


def test_every_consumer_ships_stats_lib_by_interpolating_the_fact():
    """Render each consumer's ship list with the include's own fact and check the ConfigMap
    key lands where the pod imports from — the accept half against the real tree."""
    for name in sorted(EXPECTED_CONSUMERS):
        role = _role(name)
        cmds = configmap_cmds(role)
        assert cmds, f"{name}: no `create configmap --from-file` task found"
        assert all(ships_through_the_fact(cmd) for cmd in cmds), (
            f"{name}'s ship list must interpolate {{{{ {FACT} }}}} and never spell "
            f"{MODULE}.py itself: {cmds}"
        )
        assert not hand_spellings(role), hand_spellings(role)
        (dest_dir,) = [v[DEST_VAR] for v in includes_of(role)]
        _, fact_value = stage_contract(dest_dir)
        rendered = (
            Environment()
            .from_string(cmds[0])
            .render({FACT: fact_value, "k8s_namespace": "ns"})
        )
        assert f"--from-file={MODULE}.py={dest_dir}/{MODULE}.py" in rendered, rendered


def test_the_include_runs_before_the_consumer_renders_its_configmap():
    """The fact is set by the include, so the include must precede the render in run order."""
    for name in sorted(EXPECTED_CONSUMERS):
        tasks = leaf_tasks(load_tasks(_role(name) / "tasks" / "main.yml"))
        include_at = next(i for i, t in enumerate(tasks) if _is_include(t))
        render_at = next(
            i for i, t in enumerate(tasks) if "create configmap" in _shell_cmd(t)
        )
        assert include_at < render_at, f"{name}: the include sits after the render"


def test_a_hand_spelled_or_missing_lib_entry_is_flagged(tmp_path):
    """The rejecting half on a synthetic consumer, in both shapes the include removed: the
    entry spelled by hand (the drift), and no entry at all (staged but not mounted)."""
    role = tmp_path / "synthetic"
    (role / "tasks").mkdir(parents=True)
    head = (
        "---\n- name: Render the script ConfigMap manifest\n  ansible.builtin.shell:\n"
        "    cmd: >-\n      k3s kubectl create configmap synthetic-script\n"
        "      --from-file=synthetic.py=/etc/rancher/k3s/synthetic/synthetic.py\n"
    )
    tail = (
        "      --dry-run=client -o yaml > /etc/rancher/k3s/synthetic/configmap.yaml\n"
    )

    (role / "tasks" / "main.yml").write_text(
        head
        + "      --from-file=stats_lib.py=/etc/rancher/k3s/synthetic/stats_lib.py\n"
        + tail
    )
    (cmd,) = configmap_cmds(role)
    assert not ships_through_the_fact(cmd)
    assert hand_spellings(role) == [
        "synthetic/tasks/main.yml: Render the script ConfigMap manifest"
    ]

    (role / "tasks" / "main.yml").write_text(head + tail)
    (cmd,) = configmap_cmds(role)
    assert not ships_through_the_fact(cmd)
    assert hand_spellings(role) == []

    (role / "tasks" / "main.yml").write_text(head + f"      {{{{ {FACT} }}}}\n" + tail)
    (cmd,) = configmap_cmds(role)
    assert ships_through_the_fact(cmd)


def test_a_hand_written_stats_lib_copy_is_flagged(tmp_path):
    """A `copy:` of the file outside the include is the other hand spelling."""
    role = tmp_path / "backslider"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "---\n- name: Install the scripts\n  ansible.builtin.copy:\n"
        '    src: "{{ item }}"\n    dest: "/etc/rancher/k3s/backslider/{{ item | basename }}"\n'
        "    mode: '0644'\n  loop:\n    - reader.py\n"
        '    - "{{ role_path }}/../game-stats-lib/files/stats_lib.py"\n'
    )
    assert hand_spellings(role) == ["backslider/tasks/main.yml: Install the scripts"]


def test_the_shared_task_file_carries_no_tags():
    """Tags UNION in Ansible, so a tag here would also inherit the caller's — matching
    install_host_lib.yml, release_bin.yml, stamp_render.yml and stamp_deployed.yml."""
    for task in walk_tasks(
        load_tasks(K8S_ROLES / "game-stats-lib" / "tasks" / "stage.yml")
    ):
        assert "tags" not in task, task
