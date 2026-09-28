#!/usr/bin/env python3
"""stats_lib.py reaches each game's pod through the include, never through a hand-written entry.

`roles/k8s/game-stats/files/stats_lib.py` is the skeleton the terraria and valheim exporters
share. Each game ships it into its own ConfigMap that a python:3.14-alpine pod mounts beside the
entry script, so each game needs two things: the file staged on the node, and a `--from-file`
entry naming it in that game's own `kubectl create configmap` command. Until #2055 those were two
hand-kept halves, and a game could do one without the other and ship a pod that dies at
`import stats_lib` on its next roll.

Now the include owns both halves: `tasks/stage.yml` copies the file AND sets
`game_stats_lib_from_file` to the `--from-file` argument for that copy, and each game's task
file interpolates the fact into its ship list. Since #2813 the lib and both games are one role,
so the consumers are task files, not roles. What is left to check is the include's contract:

  - the fact names exactly the file the copy task stages (key == basename, path == dest);
  - every consumer task file interpolates the fact, and none spells `stats_lib.py` by hand;
  - the task files that include `stage.yml` are exactly the ones that stage a script importing
    stats_lib, and no role outside game-stats imports it at all — nothing there could ship it.

Run: uv run pytest ansible/tests/k8s/test_game_stats_lib_ships_through_the_include.py
"""

import ast
from pathlib import Path

from jinja2 import Environment

from _helpers import K8S_ROLES, imported_module_ids, leaf_tasks, load_tasks, walk_tasks

MODULE = "stats_lib"
OWNER_ROLE = "game-stats"
SHARED_TASK = "stage.yml"
DEST_VAR = "game_stats_lib_dest_dir"
FACT = "game_stats_lib_from_file"

# The census's non-vacuity assertion — see the repo-root CLAUDE.md on why an `all()` over an
# empty set from a moved/renamed file is a false green. Compared with `==`, not `<=`, so a new
# consumer that forgets the include is exactly what this exists to catch.
EXPECTED_IMPORTERS = frozenset({"stats.py", "valheim_stats.py"})
EXPECTED_CONSUMERS = frozenset({"terraria.yml", "valheim.yml"})


def _role_dirs(roles_root: Path) -> list[Path]:
    return sorted(d for d in roles_root.iterdir() if d.is_dir())


def _task_files(role: Path) -> list[Path]:
    return sorted((role / "tasks").rglob("*.yml")) if (role / "tasks").is_dir() else []


def _imports_the_module(module: Path) -> bool:
    try:
        tree = ast.parse(module.read_text())
    except SyntaxError:
        return False
    return bool(imported_module_ids(tree, {MODULE}))


def importing_scripts(role: Path) -> set[str]:
    """Basenames of this role's `files/` scripts that import stats_lib, the lib excluded."""
    files = role / "files"
    if not files.is_dir():
        return set()
    return {
        m.name
        for m in files.rglob("*.py")
        if m.name != f"{MODULE}.py" and _imports_the_module(m)
    }


def importers(roles_root: Path) -> set[str]:
    """Role names, other than the owner, whose shipped `files/` scripts import stats_lib."""
    return {
        role.name
        for role in _role_dirs(roles_root)
        if role.name != OWNER_ROLE and importing_scripts(role)
    }


def _is_include(task: dict) -> bool:
    target = task.get("ansible.builtin.import_tasks") or task.get("import_tasks")
    return isinstance(target, str) and Path(target).name == SHARED_TASK


def includes_of(task_file: Path) -> list[dict]:
    """The vars of every `import_tasks` of the shared stage file in this task file."""
    return [
        task.get("vars") or {}
        for task in walk_tasks(load_tasks(task_file))
        if _is_include(task)
    ]


def consumer_files(role: Path) -> list[Path]:
    """The role's task files that import the shared stage file."""
    return [f for f in _task_files(role) if f.name != SHARED_TASK and includes_of(f)]


def _copy(task: dict):
    copy = task.get("ansible.builtin.copy") or task.get("copy")
    return copy if isinstance(copy, dict) else None


def staged_scripts(task_file: Path) -> set[str]:
    """Basenames of the files this task file's `copy:` tasks stage."""
    return {
        Path(str(copy.get("src", ""))).name
        for task in walk_tasks(load_tasks(task_file))
        if (copy := _copy(task))
    }


def _shell_cmd(task: dict) -> str:
    shell = task.get("ansible.builtin.shell") or task.get("shell")
    return shell.get("cmd", "") if isinstance(shell, dict) else ""


def configmap_cmds(task_file: Path) -> list[str]:
    """Every `create configmap ... --from-file` shell command this task file renders."""
    return [
        cmd
        for task in walk_tasks(load_tasks(task_file))
        if "create configmap" in (cmd := _shell_cmd(task))
    ]


def hand_spellings(task_file: Path) -> list[str]:
    """Task fields that name `stats_lib.py` by hand — a `copy:` of it, or a ship-list entry.

    Reads the task fields, not the file text: the consumers' comments legitimately mention the
    file, and a text scan would flag the explanation of why the fact exists.
    """
    offenders = []
    for task in walk_tasks(load_tasks(task_file)):
        fields = [_shell_cmd(task)]
        if copy := _copy(task):
            fields += [
                str(copy.get("src", "")),
                str(copy.get("dest", "")),
                str(task.get("loop", "")),
            ]
        if any(f"{MODULE}.py" in f for f in fields):
            offenders.append(f"{task_file.name}: {task.get('name')}")
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


_OWNER = K8S_ROLES / OWNER_ROLE
_STAGE = _OWNER / "tasks" / SHARED_TASK


def stage_contract(dest_dir: str = "/etc/rancher/k3s/synthetic") -> tuple[str, str]:
    """(rendered copy dest, rendered fact value) for a caller staging into `dest_dir`."""
    copy_dest = fact_value = None
    for task in walk_tasks(load_tasks(_STAGE)):
        if copy := _copy(task):
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


# --- The census ------------------------------------------------------------------------------


def test_the_census_finds_exactly_the_known_importers():
    assert importing_scripts(_OWNER) == set(EXPECTED_IMPORTERS)


def test_no_role_outside_game_stats_imports_stats_lib():
    """stage.yml is a task file of game-stats, so no other role has a way to ship the lib."""
    assert importers(K8S_ROLES) == set()


def test_the_task_files_that_include_the_stage_are_the_ones_that_stage_an_importer():
    """Both directions at once: a task file staging an importer without the include ships a pod
    that dies at `import stats_lib`; an include beside no importer is a dead copy."""
    including = {f.name for f in consumer_files(_OWNER)}
    staging_an_importer = {
        f.name for f in _task_files(_OWNER) if staged_scripts(f) & EXPECTED_IMPORTERS
    }
    assert including == staging_an_importer == set(EXPECTED_CONSUMERS)


def test_a_synthetic_role_that_imports_stats_lib_is_flagged(tmp_path):
    """The red proof, in tmp_path: a real role directory would be picked up by ansible-lint."""
    role = tmp_path / "synthetic"
    (role / "files").mkdir(parents=True)
    (role / "files" / "reader.py").write_text("from stats_lib import run\n")
    assert importers(tmp_path) == {"synthetic"}
    (role / "files" / "reader.py").write_text("import json\n")
    assert importers(tmp_path) == set()


def test_an_include_is_found_only_under_its_own_name(tmp_path):
    task_file = tmp_path / "game.yml"
    task_file.write_text(
        "---\n- name: Install the shared stats_lib module\n"
        "  ansible.builtin.import_tasks: stage.yml\n"
        f"  vars:\n    {DEST_VAR}: /etc/rancher/k3s/synthetic\n"
    )
    assert [v[DEST_VAR] for v in includes_of(task_file)] == [
        "/etc/rancher/k3s/synthetic"
    ]
    task_file.write_text(
        "---\n- name: Something else\n  ansible.builtin.import_tasks: backstage.yml\n"
    )
    assert includes_of(task_file) == []


def test_every_include_stages_beside_its_own_script():
    """The lib has to land in the directory the consumer's own script is copied to."""
    for task_file in consumer_files(_OWNER):
        (declared,) = includes_of(task_file)
        dest_dir = declared.get(DEST_VAR)
        assert dest_dir, f"{task_file.name} includes {SHARED_TASK} without {DEST_VAR}"
        script_dests = {
            str(Path(copy["dest"]).parent)
            for task in walk_tasks(load_tasks(task_file))
            if (copy := _copy(task))
        }
        assert script_dests == {dest_dir}, (task_file.name, script_dests, dest_dir)


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
    for task_file in consumer_files(_OWNER):
        cmds = configmap_cmds(task_file)
        assert cmds, f"{task_file.name}: no `create configmap --from-file` task found"
        assert all(ships_through_the_fact(cmd) for cmd in cmds), (
            f"{task_file.name}'s ship list must interpolate {{{{ {FACT} }}}} and never "
            f"spell {MODULE}.py itself: {cmds}"
        )
        assert not hand_spellings(task_file), hand_spellings(task_file)
        (declared,) = includes_of(task_file)
        dest_dir = declared[DEST_VAR]
        _, fact_value = stage_contract(dest_dir)
        rendered = (
            Environment()
            .from_string(cmds[0])
            .render({FACT: fact_value, "k8s_namespace": "ns"})
        )
        assert f"--from-file={MODULE}.py={dest_dir}/{MODULE}.py" in rendered, rendered


def test_the_include_runs_before_the_consumer_renders_its_configmap():
    """The fact is set by the include, so the include must precede the render in run order.
    It is also overwritten by the next game's include, so each include must sit in the same
    task file as the render that reads it."""
    for task_file in consumer_files(_OWNER):
        tasks = leaf_tasks(load_tasks(task_file))
        include_at = next(i for i, t in enumerate(tasks) if _is_include(t))
        render_at = next(
            i for i, t in enumerate(tasks) if "create configmap" in _shell_cmd(t)
        )
        assert include_at < render_at, (
            f"{task_file.name}: the include sits after the render"
        )


def test_a_hand_spelled_or_missing_lib_entry_is_flagged(tmp_path):
    """The rejecting half on a synthetic consumer, in both shapes the include removed: the
    entry spelled by hand (the drift), and no entry at all (staged but not mounted)."""
    task_file = tmp_path / "synthetic.yml"
    head = (
        "---\n- name: Render the script ConfigMap manifest\n  ansible.builtin.shell:\n"
        "    cmd: >-\n      k3s kubectl create configmap synthetic-script\n"
        "      --from-file=synthetic.py=/etc/rancher/k3s/synthetic/synthetic.py\n"
    )
    tail = (
        "      --dry-run=client -o yaml > /etc/rancher/k3s/synthetic/configmap.yaml\n"
    )

    task_file.write_text(
        head
        + "      --from-file=stats_lib.py=/etc/rancher/k3s/synthetic/stats_lib.py\n"
        + tail
    )
    (cmd,) = configmap_cmds(task_file)
    assert not ships_through_the_fact(cmd)
    assert hand_spellings(task_file) == [
        "synthetic.yml: Render the script ConfigMap manifest"
    ]

    task_file.write_text(head + tail)
    (cmd,) = configmap_cmds(task_file)
    assert not ships_through_the_fact(cmd)
    assert hand_spellings(task_file) == []

    task_file.write_text(head + f"      {{{{ {FACT} }}}}\n" + tail)
    (cmd,) = configmap_cmds(task_file)
    assert ships_through_the_fact(cmd)


def test_a_hand_written_stats_lib_copy_is_flagged(tmp_path):
    """A `copy:` of the file outside the include is the other hand spelling."""
    task_file = tmp_path / "backslider.yml"
    task_file.write_text(
        "---\n- name: Install the scripts\n  ansible.builtin.copy:\n"
        '    src: "{{ item }}"\n    dest: "/etc/rancher/k3s/backslider/{{ item | basename }}"\n'
        "    mode: '0644'\n  loop:\n    - reader.py\n    - stats_lib.py\n"
    )
    assert hand_spellings(task_file) == ["backslider.yml: Install the scripts"]


def test_the_shared_task_file_carries_no_tags():
    """Tags UNION in Ansible, so a tag here would also inherit the caller's — matching
    install_host_lib.yml, release_bin.yml, stamp_render.yml and stamp_deployed.yml."""
    for task in walk_tasks(load_tasks(_STAGE)):
        assert "tags" not in task, task
