"""The deployer's stdlib reading of the render records.

`deploy_release.digest_verdict` restates `probe_lib/releases_render.py:digest_verdict`, which
the deployer cannot import, so the first test runs both over the same record pairs. The rest
cover what the deployer adds: a render proving the applied bytes at its OWN commit, and the
grouped diff the deploy-plane shadow log reads.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_deploy_release_digest.py
"""

import json
import pathlib

import pytest

import deploy_release
from diagnostics.probe_lib import releases_render

REF = "a" * 40
OTHER = "b" * 40

RELEASE = {
    "service": "web",
    "commit": OTHER,
    "host": "daniel-box",
    "manifests_digest": "d1",
    "secret_manifests": [],
    "secret_digest": "",
}
RENDER = {**RELEASE, "commit": REF, "tree_dirty": False}

PAIRS = {
    "match": (RELEASE, RENDER),
    "manifests-differ": (RELEASE, {**RENDER, "manifests_digest": "d2"}),
    "secret-digests-differ": (
        {**RELEASE, "secret_manifests": ["s.yaml"], "secret_digest": "x"},
        {**RENDER, "secret_manifests": ["s.yaml"], "secret_digest": "y"},
    ),
    "secret-lists-differ": (
        {**RELEASE, "secret_manifests": ["s.yaml"], "secret_digest": "x"},
        {**RENDER, "secret_manifests": ["t.yaml"], "secret_digest": "x"},
    ),
    "no-secret-digest": (
        {**RELEASE, "secret_manifests": ["s.yaml"]},
        {**RENDER, "secret_manifests": ["s.yaml"], "secret_digest": "x"},
    ),
    "no-secret-list": ({**RELEASE, "secret_manifests": None}, RENDER),
    "another-commit": (RELEASE, {**RENDER, "commit": OTHER}),
    "dirty": (RELEASE, {**RENDER, "tree_dirty": True}),
    "other-host": (RELEASE, {**RENDER, "host": "daniel-server"}),
    "no-host": ({**RELEASE, "host": None}, {**RENDER, "host": None}),
    "no-digest": ({**RELEASE, "manifests_digest": ""}, RENDER),
    "no-release": (None, RENDER),
    "no-render": (RELEASE, None),
}


@pytest.mark.parametrize("name", sorted(PAIRS))
def test_the_deployers_verdict_matches_the_probe_readers(name):
    release, render = PAIRS[name]
    assert (
        deploy_release.digest_verdict(release, render, REF)[0]
        == releases_render.digest_verdict(release, render, REF)[0]
    )


def test_the_parity_pairs_reach_all_three_verdicts():
    """A parity table that only ever produced UNKNOWN would agree with anything."""
    seen = {deploy_release.digest_verdict(*pair, REF)[0] for pair in PAIRS.values()}
    assert seen == {
        deploy_release.CURRENT,
        deploy_release.DRIFTED,
        deploy_release.UNKNOWN,
    }


def _write(directory: pathlib.Path, record: dict) -> None:
    directory.mkdir(exist_ok=True)
    (directory / f"{record['service']}.json").write_text(json.dumps(record))


def test_a_matching_render_proves_its_own_commit(tmp_path):
    """FLAGGED half: the render need not be of any particular ref, only of itself."""
    _write(tmp_path / "rel", RELEASE)
    _write(tmp_path / "ren", RENDER)
    assert deploy_release.render_proof("web", tmp_path / "rel", tmp_path / "ren") == REF


@pytest.mark.parametrize(
    "render",
    [
        pytest.param({**RENDER, "manifests_digest": "d2"}, id="drifted"),
        pytest.param({**RENDER, "tree_dirty": True}, id="dirty"),
        pytest.param({**RENDER, "commit": ""}, id="no-commit"),
    ],
)
def test_a_render_that_does_not_match_proves_nothing(tmp_path, render):
    _write(tmp_path / "rel", RELEASE)
    _write(tmp_path / "ren", render)
    assert (
        deploy_release.render_proof("web", tmp_path / "rel", tmp_path / "ren") is None
    )


def test_the_digest_diff_groups_every_service_by_verdict(tmp_path):
    _write(tmp_path / "rel", RELEASE)
    _write(tmp_path / "ren", RENDER)
    _write(tmp_path / "rel", {**RELEASE, "service": "api"})
    _write(tmp_path / "ren", {**RENDER, "service": "api", "manifests_digest": "d2"})
    _write(tmp_path / "rel", {**RELEASE, "service": "db"})
    (tmp_path / "rel" / "web.previous.json").write_text(json.dumps(RELEASE))
    assert deploy_release.digest_diff(REF, tmp_path / "rel", tmp_path / "ren") == {
        "current": ["web"],
        "drifted": ["api"],
        "unknown: no render record": ["db"],
    }


def test_the_render_dir_matches_the_manifests_role():
    """A drifted path would leave every shared line on the record-only rule, silently."""
    defaults = (
        pathlib.Path(__file__).resolve().parents[4]
        / "roles/k8s/manifests/defaults/main.yml"
    ).read_text()
    assert f"manifests_render_record_dir: {deploy_release.K8S_RENDER_DIR}" in defaults
