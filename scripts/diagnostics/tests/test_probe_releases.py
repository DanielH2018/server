"""Tests for `probe.py releases` -- the reader over the k8s release records.

Every rule here is a `..._is_clean` / `..._is_flagged` pair. A check observed only from the
passing side is indistinguishable from one that fires on nothing, and this repo has paid for
that twice (volume-claim's short-circuit, image-smoke's bare-boot rule).

Run: uv run pytest scripts/diagnostics/tests/test_probe_releases.py
"""

import json
import os
import re
import subprocess
from pathlib import Path

from diagnostics.probe_lib import releases as pr

REPO = Path(__file__).resolve().parents[3]

# git subprocesses in the fixtures below must see ONLY the tmp repo they're pointed at via
# `cwd`. `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE` and friends override that from the
# environment, and prek's own `pytest` hook runs THIS suite from inside a `git commit` --
# with those exact vars set to ITS OWN in-progress commit. Without stripping them, `_run_git`
# was committing into the outer repo's half-built index instead of the fixture, and prek's
# commit failed on a nested "commit -q -m 'sonarr v1'" it never asked for.
_GIT_CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _record(
    service="sonarr", commit="a" * 40, dirty=False, applied="2026-08-29T20:00:00Z"
):
    return {
        "service": service,
        "commit": commit,
        "commit_short": commit[:8],
        "tree_dirty": dirty,
        "applied_at": applied,
        "render_dir": f"/etc/rancher/k3s/manifests/{service}",
        "manifests": {"deployment.yaml": "sha256:1", "service.yaml": "sha256:2"},
        "manifests_digest": "deadbeef",
        "secret_manifests": [],
    }


# ── the reader agrees with the writer about where records live ───────────────────────────────


def test_release_dir_matches_the_ansible_default():
    """A drifted path makes this reader report 'no records' on a healthy host -- silence, not an
    error, which is the failure mode a comment cannot prevent."""
    defaults = (REPO / "ansible/roles/k8s/manifests/defaults/main.yml").read_text()
    m = re.search(r"^manifests_release_dir:\s*(\S+)\s*$", defaults, re.M)
    assert m, "manifests_release_dir is not defined in the manifests role defaults"
    assert m.group(1) == str(pr.RELEASE_DIR)


# ── flags ────────────────────────────────────────────────────────────────────────────────────


def _row(text, service):
    """The service's own line.

    The footer legend names both flags, so asserting against the whole rendering would score every
    clean table as flagged.
    """
    return next(line for line in text.splitlines() if line.startswith(service))


def test_clean_merged_record_is_clean():
    text, code = pr.format_records([_record()], merged={"a" * 40})
    assert code == 0
    assert _row(text, "sonarr").endswith("-")


def test_dirty_record_is_flagged():
    text, code = pr.format_records([_record(dirty=True)], merged={"a" * 40})
    assert code == 1
    assert "dirty" in _row(text, "sonarr")


def test_unmerged_record_is_flagged():
    text, code = pr.format_records([_record()], merged=set())
    assert code == 1
    assert "unmerged" in _row(text, "sonarr")


def test_unparseable_record_is_flagged():
    text, code = pr.format_records(
        [{"service": "sonarr", "error": "boom"}], merged=set()
    )
    assert code == 1
    assert "UNREADABLE" in text


def test_no_records_is_its_own_exit_code():
    """Distinct from 'records exist and are clean' -- an empty directory means the stamp has
    never run, which is a different thing to report than a green fleet."""
    text, code = pr.format_records([], merged=set())
    assert code == 2
    assert "no release records" in text


def test_stale_record_is_flagged():
    text, code = pr.format_records(
        [_record()], merged={"a" * 40}, stale={"sonarr": "changed since applied: x"}
    )
    assert code == 1
    assert "stale" in _row(text, "sonarr")


def test_clean_record_ignores_unrelated_stale_entry():
    """`stale` is keyed by service; a flag for a DIFFERENT service must not leak onto this row --
    the false-GREEN this whole feature exists to close would become a false-RED if it did."""
    text, code = pr.format_records(
        [_record("sonarr")], merged={"a" * 40}, stale={"radarr": "changed"}
    )
    assert code == 0
    assert _row(text, "sonarr").endswith("-")


# ── per-service lookup ───────────────────────────────────────────────────────────────────────


def test_named_service_returns_its_full_record():
    text, code = pr.format_records(
        [_record("sonarr"), _record("radarr")], merged={"a" * 40}, service="radarr"
    )
    assert code == 0
    assert json.loads(text)["service"] == "radarr"


def test_unknown_service_is_flagged():
    _, code = pr.format_records([_record("sonarr")], merged={"a" * 40}, service="nope")
    assert code == 2


# ── loading ──────────────────────────────────────────────────────────────────────────────────


def test_load_records_reads_and_orders_newest_first(tmp_path):
    (tmp_path / "sonarr.json").write_text(
        json.dumps(_record("sonarr", applied="2026-01-01T00:00:00Z"))
    )
    (tmp_path / "radarr.json").write_text(
        json.dumps(_record("radarr", applied="2026-06-01T00:00:00Z"))
    )
    got = pr.load_records(tmp_path)
    assert [r["service"] for r in got] == ["radarr", "sonarr"]


def test_load_records_skips_the_previous_files(tmp_path):
    """`*.previous.json` also matches `*.json`; without the guard every service would appear
    twice, once with a stale commit, and the table would read as drift that is not there."""
    (tmp_path / "sonarr.json").write_text(json.dumps(_record("sonarr")))
    (tmp_path / "sonarr.previous.json").write_text(json.dumps(_record("sonarr")))
    assert [r["service"] for r in pr.load_records(tmp_path)] == ["sonarr"]


def test_load_records_reports_a_truncated_record(tmp_path):
    """A half-written record must surface, not vanish -- a truncated write is exactly the case
    where a silently shorter table is worst."""
    (tmp_path / "sonarr.json").write_text('{"service": "sonarr"')
    got = pr.load_records(tmp_path)
    assert len(got) == 1 and "error" in got[0]


def test_missing_directory_is_empty_not_an_exception(tmp_path):
    assert pr.load_records(tmp_path / "absent") == []


# ── stale: git fixture repos ────────────────────────────────────────────────────────────────
#
# Each fixture is a throwaway repo, never a real commit to this checkout -- signing and identity
# are stubbed locally so the test needs no signing key. `refs/remotes/origin/master` is set with
# `update-ref` rather than a real remote, which is all `compute_stale`'s `ref="origin/master"`
# default reads.


def _run_git(repo, *args):
    subprocess.run(
        ["git", *args],
        cwd=repo,
        env=_GIT_CLEAN_ENV,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(repo):
    repo.mkdir(parents=True, exist_ok=True)
    _run_git(repo, "init", "-q", "-b", "master")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test")
    _run_git(repo, "config", "commit.gpgsign", "false")


def _commit(repo, files, message):
    """Write `files` ({relative path: content}), commit, and return the new SHA."""
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", message)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        env=_GIT_CLEAN_ENV,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _set_origin_master(repo, sha):
    _run_git(repo, "update-ref", "refs/remotes/origin/master", sha)


def test_named_role_is_stale_after_its_own_role_changes(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _commit(
        repo,
        {"ansible/roles/k8s/sonarr/templates/deployment.yaml.j2": "v1\n"},
        "sonarr v1",
    )
    tip = _commit(
        repo,
        {"ansible/roles/k8s/sonarr/templates/deployment.yaml.j2": "v2\n"},
        "sonarr v2",
    )
    _set_origin_master(repo, tip)

    stale = pr.compute_stale(
        [_record("sonarr", commit=base)], repo_root=repo, shared_roles=set()
    )
    assert "sonarr" in stale
    assert "deployment.yaml.j2" in stale["sonarr"]


def test_named_role_is_clean_when_its_record_is_the_tip(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(
        repo,
        {"ansible/roles/k8s/sonarr/templates/deployment.yaml.j2": "v1\n"},
        "sonarr v1",
    )
    tip = _commit(
        repo,
        {"ansible/roles/k8s/sonarr/templates/deployment.yaml.j2": "v2\n"},
        "sonarr v2",
    )
    _set_origin_master(repo, tip)

    stale = pr.compute_stale(
        [_record("sonarr", commit=tip)], repo_root=repo, shared_roles=set()
    )
    assert stale == {}


def test_shared_role_change_marks_the_consuming_service_stale(tmp_path):
    """Without widening onto the shared role, a `manifests`-only change reads clean for every
    service -- the exact false-GREEN issue #947 names."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _commit(
        repo,
        {
            "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2": "v1\n",
            "ansible/roles/k8s/manifests/tasks/main.yml": "v1\n",
        },
        "baseline",
    )
    tip = _commit(
        repo,
        {"ansible/roles/k8s/manifests/tasks/main.yml": "v2\n"},
        "shared role changes, sonarr's own role does not",
    )
    _set_origin_master(repo, tip)
    record = [_record("sonarr", commit=base)]

    without_widening = pr.compute_stale(record, repo_root=repo, shared_roles=set())
    assert without_widening == {}, (
        "reproduces the false-GREEN this feature exists to close"
    )

    with_widening = pr.compute_stale(record, repo_root=repo, shared_roles={"manifests"})
    assert "sonarr" in with_widening
    assert "manifests/tasks/main.yml" in with_widening["sonarr"]


def test_docs_and_test_only_changes_do_not_mark_a_service_stale(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _commit(
        repo,
        {"ansible/roles/k8s/sonarr/templates/deployment.yaml.j2": "v1\n"},
        "sonarr v1",
    )
    tip = _commit(
        repo,
        {
            "ansible/roles/k8s/sonarr/README.md": "docs\n",
            "ansible/roles/k8s/sonarr/tests/test_something.py": "assert True\n",
        },
        "docs and a role-local test, neither deployed",
    )
    _set_origin_master(repo, tip)

    stale = pr.compute_stale(
        [_record("sonarr", commit=base)], repo_root=repo, shared_roles=set()
    )
    assert stale == {}


def test_unresolvable_commit_is_stale_not_skipped(tmp_path):
    """A commit this checkout has never seen (a pruned branch, a shallow clone) must read as
    stale, not silently pass -- a record naming an unknown commit is not evidence of anything."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    tip = _commit(
        repo,
        {"ansible/roles/k8s/sonarr/templates/deployment.yaml.j2": "v1\n"},
        "sonarr v1",
    )
    _set_origin_master(repo, tip)

    stale = pr.compute_stale(
        [_record("sonarr", commit="f" * 40)], repo_root=repo, shared_roles=set()
    )
    assert stale.get("sonarr") == "commit unknown to this checkout"


# ── missing_services: no record at all ──────────────────────────────────────────────────────


def _role(roles_dir, name, *, consumes_manifests):
    tasks = roles_dir / name / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    body = (
        "- include_role:\n    name: k8s/manifests\n"
        if consumes_manifests
        else "- debug:\n"
    )
    (tasks / "main.yml").write_text(body)


def _host_vars_with(tmp_path, entries):
    host_vars = tmp_path / "host_vars"
    host_vars.mkdir(parents=True, exist_ok=True)
    lines = ["containers_list:"]
    for name in entries:
        lines.append(f"  - name: {name}")
        lines.append("    platform: k8s")
    (host_vars / "fakehost.yml").write_text("\n".join(lines) + "\n")
    return host_vars


def test_service_with_no_record_reads_unknown(tmp_path):
    roles_dir = tmp_path / "roles"
    _role(roles_dir, "sonarr", consumes_manifests=True)
    _role(roles_dir, "radarr", consumes_manifests=True)
    host_vars = _host_vars_with(tmp_path, ["sonarr", "radarr"])

    missing = pr.missing_services(
        [_record("sonarr")], host_vars=host_vars, k8s_roles_dir=roles_dir
    )
    assert missing == ["radarr"]


def test_service_present_in_records_is_not_missing(tmp_path):
    roles_dir = tmp_path / "roles"
    _role(roles_dir, "sonarr", consumes_manifests=True)
    host_vars = _host_vars_with(tmp_path, ["sonarr"])

    missing = pr.missing_services(
        [_record("sonarr")], host_vars=host_vars, k8s_roles_dir=roles_dir
    )
    assert missing == []


def test_a_role_that_never_applies_manifests_is_never_missing(tmp_path):
    """The n8n-images shape: a `containers_list` k8s entry whose role only builds an image
    (`k8s/image-builder`) and never includes `k8s/manifests`, so it is never release-stamped and
    must not read as permanently missing -- 'a monitor nobody trusts is worse than none'."""
    roles_dir = tmp_path / "roles"
    _role(roles_dir, "image-only-role", consumes_manifests=False)
    host_vars = _host_vars_with(tmp_path, ["image-only-role"])

    missing = pr.missing_services([], host_vars=host_vars, k8s_roles_dir=roles_dir)
    assert missing == []


# ── non-vacuity: the census this depends on must still find real roles ─────────────────────


def test_shared_k8s_roles_matches_the_known_set():
    """`shared_k8s_roles()` must keep finding the roles with no `containers_list` entry -- a
    directory move or an empty `ansible/roles/k8s/` would otherwise return an empty set and
    every consumer of this function would go quiet rather than fail loudly."""
    assert pr.shared_k8s_roles() == {
        "cronjob-gate",
        "game-stats-lib",
        "image-builder",
        "longhorn-api",
        "manifests",
        "rollout-drain",
        "volume-claim",
        "volume-revert",
        "volume-snapshot",
    }


def test_consumes_manifests_agrees_with_the_real_tree():
    """sonarr applies manifests; n8n-images (image-builder only) does not -- the exact pair this
    repo hit live while building this feature."""
    assert pr._consumes_manifests(REPO / "ansible/roles/k8s/sonarr") is True
    assert pr._consumes_manifests(REPO / "ansible/roles/k8s/n8n-images") is False
