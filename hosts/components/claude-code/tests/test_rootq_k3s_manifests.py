"""rootq's answers for the registry-gc component's rows under the k3s manifests tree (#6988).

rootq once answered these rows UNCHECKED, because the whole tree is on the
deny list. The tree stays denied, since it holds every staged role's secret.yaml, so PATHS
names each row exactly and stat alone answers it. tests/test_rootq.py's rig roots paths under
the home, where the anchored `/etc/rancher/k3s/*` pattern cannot match, so these run with no
environment against the live paths. A listed path gets past the deny list to the existence
check, which every test host fails, while a neighbour stays refused as denied.

Run: uv run pytest hosts/components/claude-code/tests/test_rootq_k3s_manifests.py
"""

import os

import homelab_rootq as rootq
import pytest

MANIFESTS = "/etc/rancher/k3s/manifests"
LISTED = [
    f"{MANIFESTS}/registry-jobs",
    f"{MANIFESTS}/registry-jobs/gc-job.yaml",
    f"{MANIFESTS}/registry-jobs/ghcr-mirror-gc-job.yaml",
    f"{MANIFESTS}/registry-jobs/dockerhub-mirror-gc-job.yaml",
]
UNLISTED = [
    f"{MANIFESTS}/registry/secret.yaml",
    f"{MANIFESTS}/registry-jobs/selftest-push-job.yaml",
    "/etc/rancher/k3s/registries.yaml",
]
HEX = "0" * 64


def run_live(capsys, *argv: str) -> tuple[int, str, str]:
    rc = rootq.main(list(argv), {})
    out, err = capsys.readouterr()
    return rc, out, err


@pytest.mark.parametrize("live", LISTED)
def test_stat_of_a_listed_k3s_manifest_path_gets_past_the_deny_list(capsys, live):
    rc, out, err = run_live(capsys, "stat", live)
    assert (rc, out) == (2, "")
    assert err == f"refused: {live} does not exist\n"


@pytest.mark.parametrize("live", UNLISTED)
def test_stat_of_an_unlisted_k3s_manifest_path_is_refused_as_denied(capsys, live):
    rc, out, err = run_live(capsys, "stat", live)
    assert (rc, out) == (2, "")
    assert "is on the deny list and not on the allowlist" in err


@pytest.mark.parametrize("argv", [["cat"], ["sha256"], ["matches", HEX]])
def test_a_listed_k3s_manifest_refuses_every_verb_but_stat(capsys, argv):
    live = f"{MANIFESTS}/registry-jobs/gc-job.yaml"
    rc, out, err = run_live(capsys, argv[0], live, *argv[1:])
    assert (rc, out) == (2, "")
    assert "deny list" in err


# The operator's #7007 ruling: the two mirror GC Jobs hold no secret, so they leave DENY by
# exact name and a caller can ask `matches` of them.
EXEMPT = [
    f"{MANIFESTS}/registry-jobs/ghcr-mirror-gc-job.yaml",
    f"{MANIFESTS}/registry-jobs/dockerhub-mirror-gc-job.yaml",
]


@pytest.mark.parametrize("argv", [["sha256"], ["matches", HEX]])
@pytest.mark.parametrize("live", EXEMPT)
def test_an_exempt_gc_job_gets_past_the_deny_list_for_every_tier1_verb(
    capsys, live, argv
):
    rc, out, err = run_live(capsys, argv[0], live, *argv[1:])
    assert (rc, out) == (2, "")
    assert err == f"refused: {live} does not exist\n"


def test_the_exemption_covers_the_two_exact_paths_and_not_their_tree() -> None:
    assert [p for p in EXEMPT if rootq.denied(p)] == []
    for path in [
        f"{MANIFESTS}/registry-jobs",
        f"{MANIFESTS}/registry-jobs/gc-job.yaml",
    ]:
        assert rootq.denied(path), path
    assert rootq.denied(EXEMPT[0] + ".bak")


# `deny=yes` marks a credential file, which a caller must not ask `matches` of; an exempt
# path's stat line must carry size and mtime instead.
@pytest.mark.parametrize("live", EXEMPT)
def test_stat_of_an_exempt_gc_job_prints_size_and_no_deny_marker(tmp_path, live):
    (tmp_path / "job.yaml").write_text("kind: Job\n")
    out = rootq.describe(live, os.lstat(tmp_path / "job.yaml"))
    assert "size=10 mtime=" in out and "deny=" not in out, out


def test_stat_of_a_denied_neighbour_still_prints_the_deny_marker(tmp_path):
    (tmp_path / "job.yaml").write_text("kind: Job\n")
    out = rootq.describe(
        f"{MANIFESTS}/registry-jobs/gc-job.yaml", os.lstat(tmp_path / "job.yaml")
    )
    assert out.endswith(" deny=yes\n") and "size=" not in out, out
