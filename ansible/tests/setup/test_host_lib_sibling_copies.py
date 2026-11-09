#!/usr/bin/env python3
"""Every role whose shipped scripts `import host_lib` must install it through the shared task.

`hosts/lib/host_lib.py` has no single home on a host. Each consumer is a
directly-invoked script, so it gets its own directory on `sys.path` and nothing else, and the
file has to be copied in beside it. A role that does that goes through
`roles/setup/common/tasks/install_host_lib.yml`, which owns the source path, the `host_lib.py`
basename and the 0755 mode.

WHAT THIS GUARD IS FOR. The shared task file cannot enforce its own adoption: a role that stops
including it still deploys, still passes ansible-lint, and dies at `import host_lib` on its next
cron — on the host, hours later, with the traceback in a journal nobody is reading. The census
below is the only thing that notices. It is derived by AST from `files/**/*.py`, not from a
list someone maintains, so a new consumer is in scope the moment it imports the module.

WHY THE SHARED FILE DOES NOT WRITE THE STAMP PAIR, and this test asserts it instead. A
`stamp_deployed` fragment is named per group and holds that group's whole pair list, so a
second fragment written from inside the shared file would overwrite the caller's. Each
site records a host_lib pair in a group their own role declares, and which pairs belong in that
group is the caller's knowledge, not the shared file's. The copy and the pair are therefore
joined here rather than in the task file, in both directions:
`test_a_declared_stamp_pair_names_a_directory_the_include_installs_into` catches a pair naming the
wrong directory, and `test_every_consumer_that_stamps_its_code_also_stamps_host_lib_is_clean`
catches a pair that is missing.

Run: uv run pytest ansible/tests/setup/test_host_lib_sibling_copies.py
"""

import ast
from pathlib import Path

from _helpers import ROLES, load_tasks, walk_tasks
from _role_census import every_plane_role_dirs, role_task_files
from lib.role_installs import runs_on_no_host

MODULE = "host_lib"
SHARED_TASK = "common/tasks/install_host_lib.yml"
HOST_LIB_SRC = "hosts/lib/host_lib.py"
# The census's non-vacuity assertion. A scan that finds its own subject by glob returns an empty
# set the moment those files move, and an `all()` over nothing passes. Naming the members
# means the failure says WHICH role went missing rather
# than that a count moved. Compared with `==`, not `<=`: a new consumer that forgets the include
# is exactly what this exists to catch, and `<=` would wave it through.
EXPECTED_CONSUMERS = frozenset(
    {
        "gitops_deploy",
    }
)


# A consumer whose role ends on every host installs nothing, so it owes no include or stamp
# pair. gitops_deploy is the one: its `files/` still import host_lib until the deployer's code is
# deleted, and the role's only task is a `meta: end_role`.
def live_consumers() -> list[str]:
    """The EXPECTED_CONSUMERS whose role runs on some host, sorted."""
    return sorted(n for n in EXPECTED_CONSUMERS if not runs_on_no_host(_role(n)))


# Consumers whose script runs in a pod, mapped to the template that stages host_lib.py into the
# pod's ConfigMap. No host copy exists for them, so no include applies. configarr's and
# janitorr's health readers moved from daniel-box crons into CronJobs at H2.5b and H2.5c.
POD_STAGED_CONSUMERS = {
    "configarr": "k8s/configarr/templates/health-configmap.yaml.j2",
    "janitorr": "k8s/janitorr/templates/health-configmap.yaml.j2",
}

# roles/setup/common OWNS host_lib.py; it does not import it as a sibling. Excluded by name
# rather than by a path heuristic so the exemption is visible.
OWNER_ROLE = "common"


def _imports_host_lib(source: str) -> bool:
    """True when the module imports host_lib, in either spelling.

    AST rather than a substring match: `host_lib` appears in prose comments under
    roles/k8s/monitor-bridge/files/bridge/, which is not a consumer and must not be counted.
    """
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


def consumers(roles_root: Path) -> set[str]:
    """Role names whose shipped `files/` scripts import host_lib."""
    found = set()
    for role in every_plane_role_dirs(roles_root):
        if role.name == OWNER_ROLE:
            continue
        files = role / "files"
        if not files.is_dir():
            continue
        for module in files.rglob("*.py"):
            if _imports_host_lib(module.read_text()):
                found.add(role.name)
                break
    return found


def includes_of(role: Path) -> list[dict]:
    """Every `import_tasks` of the shared install file in this role, with its vars.

    Walks nested task files (`tasks/sub/*.yml`), not just the top level — a role can
    `import_tasks` a sub-file that itself includes the shared install task.
    """
    out = []
    for task_file in role_task_files(role):
        for task in walk_tasks(load_tasks(task_file)):
            target = task.get("ansible.builtin.import_tasks") or task.get(
                "import_tasks"
            )
            if isinstance(target, str) and target.endswith(SHARED_TASK):
                out.append(task.get("vars") or {})
    return out


def stamp_pairs_of(role: Path) -> list[dict]:
    """Every declared stamp_deployed pair in this role, across all its task files, nested too."""
    out = []
    for task_file in role_task_files(role):
        for task in walk_tasks(load_tasks(task_file)):
            pairs = (task.get("vars") or {}).get("stamp_deployed_pairs")
            if isinstance(pairs, list):
                out.extend(p for p in pairs if isinstance(p, dict))
    return out


def unstamped_host_lib_installs(role: Path) -> list[str]:
    """host_lib destinations this role installs into but does not record a stamp pair for.

    Compares the templated strings as written, not resolved paths: `includes_of` and
    `stamp_pairs_of` both read raw task vars, so a role passing `{{ fake_remux_opt_dir }}` has to
    spell the pair the same way. That is the same comparison
    `test_a_declared_stamp_pair_names_a_directory_the_include_installs_into` makes in reverse.
    """
    installed = {
        (f"{v['host_lib_dir']}/host_lib.py", HOST_LIB_SRC)
        for v in includes_of(role)
        if v.get("host_lib_dir")
    }
    stamped = {
        (pair["live"], pair.get("src"))
        for pair in stamp_pairs_of(role)
        if pair.get("live")
    }
    return sorted(live for live, _ in installed - stamped)


def hand_copies(roles_root: Path) -> list[str]:
    """Task files that still `copy:` host_lib.py themselves instead of including the shared task.

    Rooted like `consumers()` so the rejecting half can build its offender in tmp_path. Reads
    `src`, `dest` and `loop` together because a hand copy can spell it three
    ways: a dedicated task with the path in `src`, a loop item, and a `dest` naming the file.
    Walks nested task files too (`tasks/sub/*.yml`), skipping only the shared install task
    itself — the one file that legitimately copies host_lib.py — rather than every task file
    OWNER_ROLE ships, so a hand copy planted elsewhere in that role is still caught.
    """
    offenders = []
    for role in every_plane_role_dirs(roles_root):
        for task_file in role_task_files(role):
            if role.name == OWNER_ROLE and task_file.name == "install_host_lib.yml":
                continue
            for task in walk_tasks(load_tasks(task_file)):
                copy = task.get("ansible.builtin.copy") or task.get("copy")
                if not isinstance(copy, dict):
                    continue
                haystack = f"{copy.get('src', '')} {copy.get('dest', '')} {task.get('loop', '')}"
                if "host_lib.py" in haystack:
                    offenders.append(str(task_file.relative_to(roles_root)))
    return sorted(set(offenders))


def _role(name: str) -> Path:
    matches = [d for d in every_plane_role_dirs(ROLES) if d.name == name]
    assert len(matches) == 1, (
        f"expected one role directory named {name!r}, found {matches}"
    )
    return matches[0]


# --- The census ------------------------------------------------------------------------------


def test_the_census_finds_exactly_the_known_consumers():
    assert consumers(ROLES) == (set(EXPECTED_CONSUMERS) | set(POD_STAGED_CONSUMERS))


def test_an_emptied_consumer_owes_no_include_and_a_live_one_does():
    assert "gitops_deploy" in EXPECTED_CONSUMERS
    assert "gitops_deploy" not in live_consumers()
    assert runs_on_no_host(_role("gitops_deploy"))
    assert not runs_on_no_host(_role("initial_setup"))


def test_every_pod_staged_consumer_stages_host_lib_into_its_configmap():
    for role, template in POD_STAGED_CONSUMERS.items():
        text = (ROLES / template).read_text()
        assert "/../hosts/lib/host_lib.py" in text, (
            f"{role} imports host_lib in a pod, but {template} no longer stages it"
        )


def test_no_pod_staged_consumer_stages_the_deleted_kuma_push_module():
    # A key left behind fails the whole role's render: its `lookup('file', ...)` has no file.
    for role, template in POD_STAGED_CONSUMERS.items():
        assert "kuma_push" not in (ROLES / template).read_text(), role


def test_the_shared_task_file_exists_where_every_caller_names_it():
    assert (ROLES / "setup" / "common" / "tasks" / "install_host_lib.yml").is_file()


def test_a_synthetic_role_that_imports_host_lib_without_the_include_is_flagged(
    tmp_path,
):
    """The red proof, built in tmp_path rather than in the tree.

    A real role directory would be picked up by ansible-lint and by
    the no-role-ships-a-test-file census row, so the proof has to live outside ROLES. That is why
    `consumers()` and `includes_of()` take their root as an argument.
    """
    role = tmp_path / "setup" / "synthetic"
    (role / "files").mkdir(parents=True)
    (role / "tasks").mkdir()
    (role / "files" / "reader.py").write_text("from host_lib import run_kubectl\n")
    (role / "tasks" / "main.yml").write_text(
        "---\n"
        "- name: Install the reader\n"
        "  ansible.builtin.copy:\n"
        "    src: reader.py\n"
        "    dest: /opt/synthetic/reader.py\n"
        "    mode: '0755'\n"
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
        "- name: Install the shared host_lib helper\n"
        '  ansible.builtin.import_tasks: "{{ role_path }}/../common/tasks/install_host_lib.yml"\n'
        "  vars:\n"
        "    host_lib_dir: /opt/synthetic\n"
    )
    assert [v["host_lib_dir"] for v in includes_of(role)] == ["/opt/synthetic"]


# --- What each include declares --------------------------------------------------------------


def test_a_consumer_that_stamps_its_scripts_but_not_host_lib_is_flagged(tmp_path):
    """The rejecting half, on a synthetic role: stamping SOMETHING is not stamping host_lib.

    Built in tmp_path for the same reason as the census's red proof — a real role directory
    would be picked up by ansible-lint and by the no-role-ships-a-test-file census row.
    """
    role = tmp_path / "setup" / "halfstamped" / "tasks"
    role.mkdir(parents=True)
    (role / "main.yml").write_text(
        "---\n"
        "- name: Install the shared host_lib helper\n"
        '  ansible.builtin.import_tasks: "{{ role_path }}/../common/tasks/install_host_lib.yml"\n'
        "  vars:\n"
        "    host_lib_dir: /opt/halfstamped\n"
        "- name: Record the deployed code\n"
        '  ansible.builtin.import_tasks: "{{ role_path }}/../common/tasks/stamp_deployed.yml"\n'
        "  vars:\n"
        "    stamp_deployed_name: halfstamped\n"
        "    stamp_deployed_pairs:\n"
        "      - live: /opt/halfstamped/reader.py\n"
        "        src: ansible/roles/setup/halfstamped/files/reader.py\n"
    )
    assert unstamped_host_lib_installs(role.parent) == ["/opt/halfstamped/host_lib.py"]

    # And the accepting half on the same role, so a detector that flagged every install fails
    # here rather than reading as strictness.
    (role / "main.yml").write_text(
        (role / "main.yml").read_text()
        + "      - live: /opt/halfstamped/host_lib.py\n"
        + f"        src: {HOST_LIB_SRC}\n"
    )
    assert unstamped_host_lib_installs(role.parent) == []


def test_the_shared_task_file_carries_no_tags():
    """Tags UNION in Ansible, so a tag here would also inherit the caller's.

    Same rule stamp_render.yml and stamp_deployed.yml state in their headers:
    an imported task must be selected by exactly the caller's tag.
    """
    shared = ROLES / "setup" / "common" / "tasks" / "install_host_lib.yml"
    for task in walk_tasks(load_tasks(shared)):
        assert "tags" not in task, task


def test_no_role_still_copies_host_lib_by_hand_is_clean():
    """Hand copies of host_lib.py must not creep back one role at a time."""
    offenders = hand_copies(ROLES)
    assert not offenders, (
        f"{offenders} copy host_lib.py by hand again. Include "
        f"{SHARED_TASK} instead — that is the file that owns the source path and the mode."
    )


def test_a_hand_written_host_lib_copy_is_flagged(tmp_path):
    """The rejecting half of the pair above.

    `hand_copies` finds nothing in the tree by construction — that is the point of the change
    it guards — so from the passing side a detector that stopped matching is indistinguishable
    from one that works. This is the only thing that tells them apart.

    Nested one level down (`tasks/sub/health.yml`), not `tasks/main.yml`: `hand_copies` must walk
    sub-files, or a hand copy tucked in an included sub-file reads as clean.
    """
    role = tmp_path / "setup" / "backslider" / "tasks"
    nested = role / "sub"
    nested.mkdir(parents=True)
    (nested / "health.yml").write_text(
        "---\n"
        "- name: Install the scripts\n"
        "  ansible.builtin.copy:\n"
        '    src: "{{ item }}"\n'
        '    dest: "/opt/backslider/{{ item | basename }}"\n'
        "    mode: '0755'\n"
        "  loop:\n"
        "    - reader.py\n"
        '    - "{{ role_path }}/../common/files/host_lib.py"\n'
    )
    assert hand_copies(tmp_path) == ["setup/backslider/tasks/sub/health.yml"]

    # And the accepting half on the same synthetic role, so a detector that flagged every copy
    # task would fail here rather than read as strictness.
    (nested / "health.yml").write_text(
        "---\n"
        "- name: Install the scripts\n"
        "  ansible.builtin.copy:\n"
        '    src: "{{ item }}"\n'
        '    dest: "/opt/backslider/{{ item | basename }}"\n'
        "    mode: '0755'\n"
        "  loop:\n"
        "    - reader.py\n"
    )
    assert hand_copies(tmp_path) == []
