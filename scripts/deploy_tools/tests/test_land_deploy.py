"""Step 4: the fallback's derivation, the verdicts of a landing with no tag, the phase's stamps.

Run: uv run pytest scripts/deploy_tools/tests/test_land_deploy.py
"""

import pytest

from _land_fakes import MERGE_SHA, PRIMARY, Fakes
from deploy_tools.land_lib import deploy
from deploy_tools.land_lib.outcome import Outcome
from deploy_tools.land_lib.landing import Classification
from deploy_tools.land_lib.tools import Classifier
from lib.git_testing import commit, git, init_repo


def _ready(landing, fakes=None, **opts):
    ln, calls = landing(fakes, **opts)
    ln.merge_sha = MERGE_SHA
    ln.ledger.t_ci = 2.0
    return ln, calls


@pytest.mark.parametrize(
    "fakes, verdict, code",
    [
        (Fakes(plane="initial_setup.yml --tags k3s"), "needs-manual-apply", 1),
        (Fakes(), "nothing-to-deploy", 0),
    ],
)
def test_no_tag_outcomes(landing, fakes, verdict, code):
    ln, _ = _ready(landing, fakes)
    ln.classification = Classification(plane=fakes.plane)
    with pytest.raises(Outcome) as exc:
        deploy.no_tag_outcome(ln)
    assert (exc.value.rc, exc.value.verdict) == (code, verdict)
    assert ln.ledger.cause == ""


def test_the_diff_fallback_derives_from_since_to_the_merge_in_the_primary_checkout(
    landing,
):
    path = "k8s/roles/sonarr/templates/deployment.yaml.j2"
    ln, calls = _ready(landing, Fakes(changed=f"{path}\n"), since="abc")
    ln.needs_diff = True
    deploy.derive_from_diff(ln)
    assert ln.resolved_tags == ["sonarr"]
    diff = ("diff", "--no-renames", "--name-only", f"abc..{MERGE_SHA}")
    assert ("git", diff, {"cwd": PRIMARY}) in calls


def test_a_role_to_role_move_derives_the_tag_of_the_role_that_lost_the_file(
    landing, tmp_path
):
    repo = init_repo(tmp_path / "repo")
    body = "".join(f"line {i}\n" for i in range(30))
    base = commit(repo, "base", **{"k8s/roles/sonarr/files/script.sh": body})
    (repo / "k8s/roles/radarr/files").mkdir(parents=True)
    git(
        repo,
        "mv",
        "k8s/roles/sonarr/files/script.sh",
        "k8s/roles/radarr/files/script.sh",
    )
    git(repo, "commit", "-q", "-m", "move", "--no-gpg-sign")
    ln, _ = _ready(landing, Fakes(), since=base)
    ln.merge_sha = "HEAD"
    ln.classifier = Classifier()
    ln.classification = Classification(declared=frozenset({"sonarr", "radarr"}))
    ln.git = lambda *args: git(repo, *args, check=False)
    ln.needs_diff = True
    deploy.derive_from_diff(ln)
    assert ln.resolved_tags == ["radarr", "sonarr"]


def test_a_diff_that_fails_ends_the_landing_naming_the_range(landing):
    ln, _ = _ready(landing, Fakes(changed_rc=128), since="abc")
    ln.needs_diff = True
    with pytest.raises(Outcome) as exc:
        deploy.derive_from_diff(ln)
    assert exc.value.rc == 1 and f"abc..{MERGE_SHA[:12]}" in exc.value.error


def test_deploy_phase_stamps_the_ledger_and_the_gated_tree(landing):
    ln, _ = _ready(landing)
    ln.flux_tags = ["sonarr"]
    deploy.deploy_phase(ln)
    assert ln.ledger.tags_label == "sonarr" and ln.ledger.t_deploy is not None
    assert ln.deployed_at == MERGE_SHA
