#!/usr/bin/env python3
"""Every role whose shipped scripts `import host_lib` must install it through the shared task.

`roles/setup/common/files/host_lib.py` has no single home on a host. Each consumer is a
directly-invoked script, so it gets its own directory on `sys.path` and nothing else, and the
file has to be copied in beside it. Seven roles do that, at eight sites, and until
`roles/setup/common/tasks/install_host_lib.yml` each wrote its own `copy:` task with its own
spelling of the source path, the `host_lib.py` basename and the 0755 mode.

WHAT THIS GUARD IS FOR. The shared task file cannot enforce its own adoption: a role that stops
including it still deploys, still passes ansible-lint, and dies at `import host_lib` on its next
cron — on the host, hours later, with the traceback in a journal nobody is reading. The census
below is the only thing that notices. It is derived by AST from `files/**/*.py`, not from a
list someone maintains, so a new consumer is in scope the moment it imports the module.

WHY THE SHARED FILE DOES NOT WRITE THE STAMP PAIR, and this test asserts it instead. A
`stamp_deployed` fragment is named per group and holds that group's whole pair list, so a
second fragment written from inside the shared file would overwrite the caller's. Four of the
eight sites record a host_lib pair and four do not; emitting one for the other four would change
host state. The copy and the pair are therefore joined here rather than in the task file:
`test_a_declared_stamp_pair_names_a_directory_the_include_installs_into`.

Run: uv run pytest ansible/tests/setup/test_host_lib_sibling_copies.py
"""

import ast
from pathlib import Path

from _helpers import ROLES, load_tasks, walk_tasks

MODULE = "host_lib"
SHARED_TASK = "common/tasks/install_host_lib.yml"
HOST_LIB_SRC = "ansible/roles/setup/common/files/host_lib.py"

# The census's non-vacuity assertion. A scan that finds its own subject by glob returns an empty
# set the moment those files move, and an `all()` over nothing passes — the repo has been bitten
# by that nine times. Naming the members means the failure says WHICH role went missing rather
# than that a count moved. Compared with `==`, not `<=`: a new consumer that forgets the include
# is exactly what this exists to catch, and `<=` would wave it through.
EXPECTED_CONSUMERS = frozenset(
    {
        "configarr",
        "fake_remux",
        "gitops_deploy",
        "janitorr",
        "k3s",
        "renovate_agent",
        "renovate_notify",
    }
)

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


def _role_dirs(roles_root: Path) -> list[Path]:
    """Every role directory under a two-level plane root (setup/, k8s/, containers/)."""
    return sorted(
        d
        for plane in sorted(roles_root.iterdir())
        if plane.is_dir()
        for d in plane.iterdir()
        if d.is_dir()
    )


def consumers(roles_root: Path) -> set[str]:
    """Role names whose shipped `files/` scripts import host_lib."""
    found = set()
    for role in _role_dirs(roles_root):
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
    """Every `import_tasks` of the shared install file in this role, with its vars."""
    out = []
    for task_file in (
        sorted((role / "tasks").glob("*.yml")) if (role / "tasks").is_dir() else []
    ):
        for task in walk_tasks(load_tasks(task_file)):
            target = task.get("ansible.builtin.import_tasks") or task.get(
                "import_tasks"
            )
            if isinstance(target, str) and target.endswith(SHARED_TASK):
                out.append(task.get("vars") or {})
    return out


def stamp_pairs_of(role: Path) -> list[dict]:
    """Every declared stamp_deployed pair in this role, across all its task files."""
    out = []
    for task_file in (
        sorted((role / "tasks").glob("*.yml")) if (role / "tasks").is_dir() else []
    ):
        for task in walk_tasks(load_tasks(task_file)):
            pairs = (task.get("vars") or {}).get("stamp_deployed_pairs")
            if isinstance(pairs, list):
                out.extend(p for p in pairs if isinstance(p, dict))
    return out


def hand_copies(roles_root: Path) -> list[str]:
    """Task files that still `copy:` host_lib.py themselves instead of including the shared task.

    Rooted like `consumers()` so the rejecting half can build its offender in tmp_path. Reads
    `src`, `dest` and `loop` together because the eight sites this replaced spelled it three
    ways: a dedicated task with the path in `src`, a loop item, and a `dest` naming the file.
    """
    offenders = []
    for role in _role_dirs(roles_root):
        if role.name == OWNER_ROLE or not (role / "tasks").is_dir():
            continue
        for task_file in sorted((role / "tasks").glob("*.yml")):
            for task in walk_tasks(load_tasks(task_file)):
                copy = task.get("ansible.builtin.copy") or task.get("copy")
                if not isinstance(copy, dict):
                    continue
                haystack = f"{copy.get('src', '')} {copy.get('dest', '')} {task.get('loop', '')}"
                if "host_lib.py" in haystack:
                    offenders.append(str(task_file.relative_to(roles_root)))
    return sorted(set(offenders))


def _role(name: str) -> Path:
    matches = [d for d in _role_dirs(ROLES) if d.name == name]
    assert len(matches) == 1, (
        f"expected one role directory named {name!r}, found {matches}"
    )
    return matches[0]


# --- The census ------------------------------------------------------------------------------


def test_the_census_finds_exactly_the_known_consumers():
    assert consumers(ROLES) == set(EXPECTED_CONSUMERS)


def test_the_shared_task_file_exists_where_every_caller_names_it():
    assert (ROLES / "setup" / "common" / "tasks" / "install_host_lib.yml").is_file()


def test_every_consumer_installs_host_lib_through_the_shared_task_is_clean():
    """The accepting half: the tree as it stands adopts the shared task everywhere."""
    missing = sorted(
        name for name in EXPECTED_CONSUMERS if not includes_of(_role(name))
    )
    assert not missing, (
        f"{missing} import host_lib but no longer include {SHARED_TASK}, so the sibling copy "
        f"is gone and the script dies at import on its next run."
    )


def test_a_synthetic_role_that_imports_host_lib_without_the_include_is_flagged(
    tmp_path,
):
    """The red proof, built in tmp_path rather than in the tree.

    A real role directory would be picked up by ansible-lint and by
    test_no_role_ships_a_test_file.py, so the proof has to live outside ROLES. That is why
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


def test_every_include_names_a_destination_directory():
    for name in sorted(EXPECTED_CONSUMERS):
        for declared in includes_of(_role(name)):
            assert declared.get("host_lib_dir"), (
                f"{name} includes {SHARED_TASK} without host_lib_dir, so the copy would land "
                f"at /host_lib.py."
            )


def test_a_declared_stamp_pair_names_a_directory_the_include_installs_into():
    """The copy↔stamp invariant the shared task file cannot hold itself.

    A role that renames its /opt directory in the include but not in `stamp_deployed_pairs`
    leaves the drift check watching a path nothing writes — armed, and checking nothing.
    """
    for name in sorted(EXPECTED_CONSUMERS):
        role = _role(name)
        installed = {f"{v['host_lib_dir']}/host_lib.py" for v in includes_of(role)}
        for pair in stamp_pairs_of(role):
            if pair.get("src") == HOST_LIB_SRC:
                assert pair["live"] in installed, (
                    f"{name} stamps {pair['live']} but installs host_lib into {sorted(installed)}"
                )


def test_a_notified_handler_exists_in_the_calling_role():
    """`--list-tasks` shows no handler wiring, so this is the only static proof it still fires."""
    for name in sorted(EXPECTED_CONSUMERS):
        role = _role(name)
        notified = [
            h
            for declared in includes_of(role)
            for h in (
                [declared["host_lib_notify"]]
                if isinstance(declared.get("host_lib_notify"), str)
                else declared.get("host_lib_notify") or []
            )
        ]
        if not notified:
            continue
        handlers = load_tasks(role / "handlers" / "main.yml")
        names = {h.get("name") for h in walk_tasks(handlers)}
        for handler in notified:
            assert handler in names, (
                f"{name} notifies {handler!r} from its host_lib include, but no handler by that "
                f"name exists in {role / 'handlers' / 'main.yml'} — the notify is a silent no-op."
            )


def test_the_shared_task_file_carries_no_tags():
    """Tags UNION in Ansible, so a tag here would also inherit the caller's.

    Same rule release_bin.yml, stamp_render.yml and stamp_deployed.yml state in their headers:
    an imported task must be selected by exactly the caller's tag.
    """
    shared = ROLES / "setup" / "common" / "tasks" / "install_host_lib.yml"
    for task in walk_tasks(load_tasks(shared)):
        assert "tags" not in task, task


def test_no_role_still_copies_host_lib_by_hand_is_clean():
    """The duplication this replaced, kept from creeping back one role at a time."""
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
    """
    role = tmp_path / "setup" / "backslider" / "tasks"
    role.mkdir(parents=True)
    (role / "main.yml").write_text(
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
    assert hand_copies(tmp_path) == ["setup/backslider/tasks/main.yml"]

    # And the accepting half on the same synthetic role, so a detector that flagged every copy
    # task would fail here rather than read as strictness.
    (role / "main.yml").write_text(
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
