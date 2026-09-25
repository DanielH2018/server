#!/usr/bin/env python3
"""configarr and janitorr must stamp every health-reader script they `copy:` onto daniel-box.

Both roles deploy host code — a reader, its verdict logic, and the shared `host_lib.py` — into
`/opt/<role>-health`, where a cron runs it. `setup-drift-lib.sh`'s deployed-code arm compares
only the pairs a role declared in `/var/lib/homelab/setup-deployed-manifest.d/`, so a script
that is copied but not declared runs stale behind a check reporting "deployed code matches the
repo" (#2590, the same shape as #2564 one role wider).

WHY A DERIVATION RATHER THAN A LIST. The copy task already names the scripts, in a `loop:` an
author edits when they add one. Reading that loop means a new reader is in scope the moment it
is added, where an enumeration here would go quiet exactly when the role grows. `_ROLES` is the
non-vacuity assertion over the derivation: it names the two roles the scan must find, so a
renamed task or a moved `/opt` directory fails by name instead of scanning nothing.

host_lib itself is guarded from the other direction by
`ansible/tests/setup/test_host_lib_sibling_copies.py`, which owns the copy<->stamp invariant
for the file every consumer shares.

Run: uv run pytest ansible/tests/services/test_health_reader_code_is_stamped.py
"""

from pathlib import Path

from _helpers import REPO, ROLES, load_tasks, walk_tasks

HOST_LIB_SRC = "ansible/roles/setup/common/files/host_lib.py"

# role name -> the /opt directory its reader runs from. Named, not derived: a role that stopped
# deploying host code would otherwise drop out of the scan silently.
_ROLES = {
    "configarr": "/opt/configarr-health",
    "janitorr": "/opt/janitorr-health",
}


def copied_into(
    tasks_dir: Path, opt_dir: str, repo_root: Path = REPO
) -> dict[str, str]:
    """live path -> the source relative to repo_root, per script a `copy:` loop ships into opt_dir.

    A loop item is either a bare basename, resolved against the role's `files/`, or a
    `{{ role_path }}/files/<name>` path that lands under the same directory — `dest` takes the
    basename either way, which is what makes both spellings comparable. `repo_root` is a
    parameter so the rejecting half below can build its offender outside the tree.
    """
    out = {}
    for task_file in sorted(tasks_dir.rglob("*.yml")):
        for task in walk_tasks(load_tasks(task_file)):
            copy = task.get("ansible.builtin.copy") or task.get("copy")
            items = task.get("loop")
            if not isinstance(copy, dict) or not isinstance(items, list):
                continue
            if opt_dir not in str(copy.get("dest", "")):
                continue
            role_dir = tasks_dir.parent
            for item in items:
                if not isinstance(item, str) or not item.endswith(".py"):
                    continue
                name = Path(item).name
                src = role_dir / "files" / name
                out[f"{opt_dir}/{name}"] = str(src.relative_to(repo_root))
    return out


def stamped_in(tasks_dir: Path) -> dict[str, str]:
    """live path -> declared source, across every stamp_deployed pair the role declares."""
    out = {}
    for task_file in sorted(tasks_dir.rglob("*.yml")):
        for task in walk_tasks(load_tasks(task_file)):
            for pair in (task.get("vars") or {}).get("stamp_deployed_pairs") or []:
                if isinstance(pair, dict) and pair.get("live"):
                    out[pair["live"]] = pair.get("src", "")
    return out


def _tasks(role: str) -> Path:
    return ROLES / "k8s" / role / "tasks"


def test_every_copied_reader_script_is_stamped_is_clean():
    for role, opt_dir in _ROLES.items():
        copied = copied_into(_tasks(role), opt_dir)
        assert copied, (
            f"no `copy:` loop in roles/k8s/{role}/tasks writes into {opt_dir} any more, so this "
            f"guard is scanning nothing — repoint it or delete it."
        )
        stamped = stamped_in(_tasks(role))
        missing = sorted(set(copied) - set(stamped))
        assert not missing, (
            f"{role} copies {missing} onto the host and declares no stamp_deployed pair for "
            f"them, so a stale copy runs behind a drift check reporting 'deployed code matches "
            f"the repo' (#2590)."
        )
        for live, src in copied.items():
            assert stamped[live] == src, (
                f"{role} stamps {live} against {stamped[live]!r}, but the copy task ships "
                f"{src!r}. The arm would compare the live file with the wrong source."
            )


def test_the_host_lib_sibling_is_stamped_too():
    """The copy comes from the shared include, so it is not in the role's own `copy:` loop."""
    for role, opt_dir in _ROLES.items():
        stamped = stamped_in(_tasks(role))
        assert stamped.get(f"{opt_dir}/host_lib.py") == HOST_LIB_SRC, (
            f"{role} does not stamp the host_lib copy the shared include installs into "
            f"{opt_dir} — the #2564 omission, in the role that inherited it."
        )


def test_every_stamped_source_exists_in_the_repo():
    """An unreadable source is reported as drift on the host, so a typo here pages an operator."""
    for role in _ROLES:
        for live, src in stamped_in(_tasks(role)).items():
            assert (REPO / src).is_file(), (
                f"{role} stamps {live} against a missing {src}"
            )


def test_a_copied_script_with_no_pair_is_flagged(tmp_path):
    """The rejecting half, on a synthetic role: the scan must be able to find an omission.

    Built in tmp_path rather than in the tree — a real role directory would be picked up by
    ansible-lint and by test_no_role_ships_a_test_file.py.
    """
    tasks = tmp_path / "k8s" / "synthetic" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text(
        "---\n"
        "- name: Install the reader scripts\n"
        "  ansible.builtin.copy:\n"
        '    src: "{{ item }}"\n'
        '    dest: "/opt/synthetic-health/{{ item | basename }}"\n'
        "  loop:\n"
        "    - reader.py\n"
        "    - reader_logic.py\n"
        "- name: Record the deployed code\n"
        '  ansible.builtin.import_tasks: "stamp_deployed.yml"\n'
        "  vars:\n"
        "    stamp_deployed_name: synthetic\n"
        "    stamp_deployed_pairs:\n"
        "      - live: /opt/synthetic-health/reader.py\n"
        "        src: ansible/roles/k8s/synthetic/files/reader.py\n"
    )
    copied = copied_into(tasks, "/opt/synthetic-health", tmp_path)
    stamped = stamped_in(tasks)
    assert sorted(set(copied) - set(stamped)) == [
        "/opt/synthetic-health/reader_logic.py"
    ]

    # And the accepting half on the same role, so a scan that flagged everything fails here
    # rather than reading as strictness.
    (tasks / "main.yml").write_text(
        (tasks / "main.yml").read_text()
        + "      - live: /opt/synthetic-health/reader_logic.py\n"
        + "        src: ansible/roles/k8s/synthetic/files/reader_logic.py\n"
    )
    assert not set(copied_into(tasks, "/opt/synthetic-health", tmp_path)) - set(
        stamped_in(tasks)
    )
