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
PATHS = [
    f"{MANIFESTS}/registry-jobs",
    f"{MANIFESTS}/registry-jobs/gc-job.yaml",
    f"{MANIFESTS}/registry-jobs/ghcr-mirror-gc-job.yaml",
    f"{MANIFESTS}/registry-jobs/dockerhub-mirror-gc-job.yaml",
    f"{MANIFESTS}/registry/secret.yaml",
]
HEX = "0" * 64


@pytest.mark.parametrize("argv", [["stat"], ["sha256"], ["matches", HEX]])
@pytest.mark.parametrize("live", PATHS)
def test_every_tier1_verb_refuses_a_k3s_manifest_path_as_denied(capsys, live, argv):
    rc = rootq.main([argv[0], live, *argv[1:]], {})
    out, err = capsys.readouterr()
    assert (rc, out) == (2, "")
    assert err.startswith(f"refused: {live} is on the deny list"), err
