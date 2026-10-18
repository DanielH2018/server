"""Each variable `k8s_host_files` shares with a k8s role or `group_vars` equals its source.

A variable both planes read stays in the k8s role's defaults (or `group_vars/all.yml`), and
`k8s_host_files` holds a copy under the same name (D5's *Host-side rows*). A change then edits
both copies, and each half routes to its own plane: the copy to the setup plane, which the
GitOps tick applies. A change that edits only one copy leaves the host and the cluster reading
different values, and no deploy reports it. This test holds every pair equal.

A copy lives in the role's `defaults/main.yml` or in the `vars:` of a task its `tasks/main.yml`
imports. docs' copy sits in task vars: role defaults are play-wide in `initial_setup.yml`, so a
defaults copy would be read by `initial_setup`'s `docs-refresh.sh.j2` as well.

The pairs are DERIVED: every copied key that a `roles/k8s/*` defaults file or
`group_vars/all.yml` also defines. `EXPECTED_PAIRS` names them, so a new pair joins the census
by name. docs' conversion PR added the first, `docs_host_site_dir`, artifacts' added four, and
media-volume's added `media_volume_host_path` with the `group_vars` keys `puid` and `pgid`,
and pi-peer-backup's flip added `pi_peer_backup_src_dir`.

docs-refresh.sh, the setup plane's site build, holds a third copy of `docs_host_site_dir` as
its `SITE_DIR`, because no setup play loads docs' defaults. The rendered script's value must
equal docs' default rendered with `sys_user`.

Red-proof: a fixture tree under `tmp_path` whose host-files copy differs from its k8s source
must be flagged, and one whose copy matches must not, for a defaults copy and a task-vars copy.
A docs-refresh script whose `SITE_DIR` differs from docs' default must be flagged.

Run: uv run pytest ansible/tests/setup/test_k8s_host_files_pairs.py
"""

import re
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
from _shell_render import rendered_shell_text
from lib.repo_paths import ALL_VARS

ROLE = SETUP_ROLES / "k8s_host_files"

# Each shared key the role copies, added by the conversion PR that moves its row.
EXPECTED_PAIRS: frozenset[str] = frozenset(
    {
        "artifacts_host_dir",
        "artifacts_peer_dir",
        "artifacts_peer_sources",
        "artifacts_stars_dir",
        "docs_host_site_dir",
        "media_volume_host_path",
        "pgid",
        "pi_peer_backup_src_dir",
        "puid",
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


def docs_refresh_drift(script: str, docs_default: str, sys_user: str) -> list[str]:
    """The mismatch between docs-refresh.sh's `SITE_DIR` and docs' default, or []."""
    match = re.search(r'^SITE_DIR="([^"]*)"$', script, re.M)
    assert match, "docs-refresh.sh no longer sets SITE_DIR"
    expected = docs_default.replace("{{ sys_user }}", sys_user)
    assert "{{" not in expected, (
        f"docs' default reads more than sys_user: {docs_default!r}"
    )
    if match[1] == expected:
        return []
    return [f"docs-refresh.sh writes {match[1]!r}, roles/k8s/docs has {expected!r}"]


def _docs_default_and_user() -> tuple[str, str]:
    docs = load_yaml(K8S_ROLES / "docs" / "defaults" / "main.yml")
    return docs["docs_host_site_dir"], load_yaml(ALL_VARS)["sys_user"]


def test_docs_refresh_writes_where_docs_mounts():
    script = rendered_shell_text("setup", "initial_setup", "docs-refresh.sh.j2")
    assert docs_refresh_drift(script, *_docs_default_and_user()) == []


def test_a_docs_refresh_site_dir_that_differs_is_flagged():
    script = 'REPO="/home/ubuntu/server"\nSITE_DIR="/srv/docs-site"\n'
    docs_default, sys_user = _docs_default_and_user()
    assert docs_refresh_drift(script, docs_default, sys_user) == [
        f"docs-refresh.sh writes '/srv/docs-site', roles/k8s/docs has "
        f"{docs_default.replace('{{ sys_user }}', sys_user)!r}"
    ]
