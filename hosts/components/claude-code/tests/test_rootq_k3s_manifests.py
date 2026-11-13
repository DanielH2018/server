"""rootq's answers under the k3s manifests tree, which holds every staged role's secret.yaml.

tests/test_rootq.py's rig roots paths under the home, where the anchored `/etc/rancher/k3s/*`
pattern cannot match, so these run with no environment against the live paths. A path off the
allowlist is refused as denied before rootq checks that it exists, so every test host answers
the same way.

Run: uv run pytest hosts/components/claude-code/tests/test_rootq_k3s_manifests.py
"""

import homelab_rootq as rootq
import pytest

MANIFESTS = "/etc/rancher/k3s/manifests"
REGISTRY_JOBS = [
    f"{MANIFESTS}/registry-jobs",
    f"{MANIFESTS}/registry-jobs/gc-job.yaml",
    f"{MANIFESTS}/registry-jobs/ghcr-mirror-gc-job.yaml",
    f"{MANIFESTS}/registry-jobs/dockerhub-mirror-gc-job.yaml",
]
MIRROR_GC_JOBS = REGISTRY_JOBS[2:]
HEX = "0" * 64


def assert_denied(capsys, live: str, *argv: str) -> None:
    rc = rootq.main([argv[0], live, *argv[1:]], {})
    out, err = capsys.readouterr()
    assert (rc, out) == (2, "")
    assert err.startswith(f"refused: {live} is on the deny list"), err


@pytest.mark.parametrize("live", REGISTRY_JOBS)
def test_stat_refuses_a_registry_jobs_path_as_denied(capsys, live):
    assert_denied(capsys, live, "stat")


@pytest.mark.parametrize("argv", [["sha256"], ["matches", HEX]])
@pytest.mark.parametrize("live", MIRROR_GC_JOBS)
def test_a_mirror_gc_job_is_not_hashed(capsys, live, argv):
    assert_denied(capsys, live, *argv)


@pytest.mark.parametrize("argv", [["stat"], ["sha256"], ["matches", HEX]])
def test_a_staged_secret_stays_refused_for_every_tier1_verb(capsys, argv):
    assert_denied(capsys, f"{MANIFESTS}/registry/secret.yaml", *argv)
