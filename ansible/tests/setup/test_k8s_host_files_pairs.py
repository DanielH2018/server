"""Each variable `k8s_host_files` shares with a k8s role or `group_vars` equals its source.

A variable both planes read stays in the k8s role's defaults (or `group_vars/all.yml`), and
`k8s_host_files` holds a copy under the same name (D5's *Host-side rows*). A change then edits
both copies, and each half routes to its own plane: the copy to the setup plane, which the
GitOps tick applies. A change that edits only one copy leaves the host and the cluster reading
different values, and no deploy reports it. This test holds every pair equal.

A copy lives in the role's `defaults/main.yml` or in the `vars:` of a task its `tasks/main.yml`
imports. Role defaults are play-wide in `initial_setup.yml`, so a defaults copy of a key another
setup role names becomes a cross-role read.

The pairs are DERIVED: every copied key that a `roles/k8s/*` defaults file or
`group_vars/all.yml` also defines. `EXPECTED_PAIRS` names them, so a new pair joins the census
by name. artifacts' conversion PR added four, and
media-volume's added `media_volume_host_path` with the `group_vars` keys `puid` and `pgid`,
and pi-peer-backup's flip added `pi_peer_backup_src_dir`, which H7.25 moved into the Pi package;
hosts/lib/tests/test_pi_tuning_component.py holds that copy equal to the k8s default.

The docs-refresh component's copy of `docs_host_site_dir` is held equal to docs' default by
`hosts/lib/tests/test_docs_refresh_component.py` since H7.10a moved the directory out of this role.

Red-proof: a fixture tree under `tmp_path` whose host-files copy differs from its k8s source
must be flagged, and one whose copy matches must not, for a defaults copy and a task-vars copy.

Run: uv run pytest ansible/tests/setup/test_k8s_host_files_pairs.py
"""

from pathlib import Path

from _helpers import (
    K8S_ROLES,
    SETUP_ROLES,
    imported_task_files,
    load_defaults,
    load_tasks,
    load_yaml,
    walk_tasks,
)
from lib.repo_paths import ALL_VARS

ROLE = SETUP_ROLES / "k8s_host_files"

# Each shared key the role copies, added by the conversion PR that moves its row.
EXPECTED_PAIRS: frozenset[str] = frozenset(
    {
        "artifacts_host_dir",
        "artifacts_peer_dir",
        "artifacts_peer_sources",
        "artifacts_stars_dir",
        "media_volume_host_path",
        "pgid",
        "puid",
        "registry_k8s_gc_cron_hour",
        "registry_k8s_gc_cron_minute",
        "registry_k8s_gc_cron_weekday",
        "registry_k8s_gc_deadline_s",
        "registry_k8s_gc_lock_file",
        "registry_k8s_gc_ttl_s",
        "registry_k8s_ghcr_mirror_claim",
        "registry_k8s_ghcr_mirror_keep_revisions",
        "registry_k8s_image",
        "registry_k8s_manage_gc",
        "registry_k8s_netpol_probe_image",
    }
)


def shared_pairs(
    role_dir: Path = ROLE, k8s_roles: Path = K8S_ROLES, all_vars: Path = ALL_VARS
) -> dict[str, list[tuple[str, object, object]]]:
    """{key: [(source, source value, copy value), ...]} for every key the role's defaults share.

    A source is a `roles/k8s/<role>` defaults file or `group_vars/all.yml`, and a key may
    have more than one.
    """
    # The role ships no defaults file until a conversion PR moves a host-only variable in.
    has_defaults = (role_dir / "defaults" / "main.yml").is_file()
    copies = load_defaults(role_dir) if has_defaults else {}
    if (role_dir / "tasks" / "main.yml").is_file():
        for task_file in imported_task_files(role_dir):
            for task in walk_tasks(load_tasks(task_file)):
                copies.update(task.get("vars") or {})
    sources = {"group_vars/all.yml": load_yaml(all_vars) or {}}
    for defaults in sorted(k8s_roles.glob("*/defaults/main.yml")):
        sources[f"roles/k8s/{defaults.parents[1].name}"] = load_yaml(defaults) or {}
    pairs: dict[str, list[tuple[str, object, object]]] = {}
    for key, copy in copies.items():
        for source, values in sources.items():
            if key in values:
                pairs.setdefault(key, []).append((source, values[key], copy))
    return pairs


def unequal_pairs(pairs: dict[str, list[tuple[str, object, object]]]) -> list[str]:
    return [
        f"{key}: {source} has {value!r}, k8s_host_files has {copy!r}"
        for key, rows in sorted(pairs.items())
        for source, value, copy in rows
        if value != copy
    ]


def test_the_live_pairs_are_exactly_the_expected_set():
    assert ROLE.is_dir(), f"no such role: {ROLE}"
    found = set(shared_pairs())
    assert found == EXPECTED_PAIRS, (
        "the k8s_host_files pair set moved: name each shared key in EXPECTED_PAIRS. "
        f"Found {sorted(found)}"
    )


def test_every_live_pair_is_equal():
    assert unequal_pairs(shared_pairs()) == []


def _fixture_tree(tmp_path: Path, source: str, copy: str) -> tuple[Path, Path, Path]:
    """A k8s role, a host-files role and an all.yml under `tmp_path`, sharing one key."""
    k8s_roles = tmp_path / "roles" / "k8s"
    (k8s_roles / "artifacts" / "defaults").mkdir(parents=True)
    (k8s_roles / "artifacts" / "defaults" / "main.yml").write_text(
        f"artifacts_host_dir: {source}\n"
    )
    role_dir = tmp_path / "roles" / "setup" / "k8s_host_files"
    (role_dir / "defaults").mkdir(parents=True)
    (role_dir / "defaults" / "main.yml").write_text(
        f"artifacts_host_dir: {copy}\nk8s_host_files_only: 1\n"
    )
    all_vars = tmp_path / "all.yml"
    all_vars.write_text("sys_user: ubuntu\n")
    return role_dir, k8s_roles, all_vars


def test_a_copy_that_differs_from_its_source_is_flagged(tmp_path):
    pairs = shared_pairs(*_fixture_tree(tmp_path, "/srv/a", "/srv/b"))
    assert set(pairs) == {"artifacts_host_dir"}
    assert unequal_pairs(pairs) == [
        "artifacts_host_dir: roles/k8s/artifacts has '/srv/a', "
        "k8s_host_files has '/srv/b'"
    ]


def test_a_copy_equal_to_its_source_is_clean(tmp_path):
    pairs = shared_pairs(*_fixture_tree(tmp_path, "/srv/a", "/srv/a"))
    assert set(pairs) == {"artifacts_host_dir"}
    assert unequal_pairs(pairs) == []


def _task_vars_tree(tmp_path: Path, copy: str) -> tuple[Path, Path, Path]:
    """The fixture tree with the copy in an imported task's `vars:` instead of defaults."""
    role_dir, k8s_roles, all_vars = _fixture_tree(tmp_path, "/srv/a", "/srv/a")
    (role_dir / "defaults" / "main.yml").write_text("k8s_host_files_only: 1\n")
    (role_dir / "tasks").mkdir()
    (role_dir / "tasks" / "main.yml").write_text(
        "- name: Artifacts\n  ansible.builtin.import_tasks: artifacts.yml\n"
    )
    (role_dir / "tasks" / "artifacts.yml").write_text(
        "- name: Ensure the directory\n  ansible.builtin.file:\n"
        "    path: '{{ artifacts_host_dir }}'\n    state: directory\n"
        f"  vars:\n    artifacts_host_dir: {copy}\n"
    )
    return role_dir, k8s_roles, all_vars


def test_a_task_vars_copy_that_differs_from_its_source_is_flagged(tmp_path):
    pairs = shared_pairs(*_task_vars_tree(tmp_path, "/srv/b"))
    assert unequal_pairs(pairs) == [
        "artifacts_host_dir: roles/k8s/artifacts has '/srv/a', "
        "k8s_host_files has '/srv/b'"
    ]


def test_a_task_vars_copy_equal_to_its_source_is_clean(tmp_path):
    pairs = shared_pairs(*_task_vars_tree(tmp_path, "/srv/a"))
    assert set(pairs) == {"artifacts_host_dir"}
    assert unequal_pairs(pairs) == []
