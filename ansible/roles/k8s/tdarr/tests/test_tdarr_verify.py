"""tdarr's inert verify Job: its script fails each broken check, and its render passes the census.

The script tests run `tdarr_verify.main` against a temporary device and library. The render
tests copy the tdarr role, move `templates/verify-inert/` to `templates/verify/` as the flip PR
will, and render the copy, so the Job is checked as Flux will receive it while the tree's own
render still emits no `tdarr-verify`. The plan is docs/target-architecture/deploy/d5-bulk-move.md,
*A verify conversion lands as inert code*.

Run: uv run pytest ansible/roles/k8s/tdarr/tests/test_tdarr_verify.py
"""

import shutil

import pytest

import tdarr_verify
from deployer_lib.deploy_cross_role import k8s_lookup_map
from lib import yaml_fast
from lib.repo_paths import REPO
from lib.k8s_roles import K8S_ROLES
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs
from renderer.tests._job_census import problems

TAG = "tdarr"


def _env(tmp_path, device=None, media=None) -> dict[str, str]:
    if device is None:
        device = tmp_path / "renderD128"
        device.write_text("")
    if media is None:
        media = tmp_path / "media"
        media.mkdir()
        (media / "tv").mkdir()
    return {
        "VERIFY_DEVICE": str(device),
        "VERIFY_MEDIA": str(media),
        "HOSTNAME": "tdarr-verify-abc",
    }


def test_a_present_device_and_a_writable_library_pass(tmp_path, capsys):
    env = _env(tmp_path)
    assert tdarr_verify.main([], env) == 0
    out = capsys.readouterr().out
    assert "renderD128 opens" in out
    assert "is writable; first entries: ['tv']" in out
    assert sorted(p.name for p in (tmp_path / "media").iterdir()) == ["tv"]


def test_a_missing_render_node_fails_the_job(tmp_path, capsys):
    env = _env(tmp_path, device=tmp_path / "absent")
    assert tdarr_verify.main([], env) == 1
    assert "cannot open the render node" in capsys.readouterr().err


def test_an_unmounted_library_fails_the_job(tmp_path, capsys):
    env = _env(tmp_path, media=tmp_path / "absent")
    assert tdarr_verify.main([], env) == 1
    assert "is not mounted" in capsys.readouterr().err


def test_a_read_only_library_fails_the_job(tmp_path, capsys):
    media = tmp_path / "media"
    media.mkdir()
    media.chmod(0o555)
    try:
        assert tdarr_verify.main([], _env(tmp_path, media=media)) == 1
    finally:
        media.chmod(0o755)
    assert "is not writable" in capsys.readouterr().err


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    return load_stubs(path)


@pytest.fixture(scope="module")
def flipped(stubs, tmp_path_factory) -> dict[str, list[dict]]:
    """The `tdarr-verify` directory a copy of the role renders once its Job leaves `verify-inert/`."""
    roles = tmp_path_factory.mktemp("roles")
    shutil.copytree(K8S_ROLES / TAG, roles / TAG)
    templates = roles / TAG / "templates"
    (templates / "verify-inert").rename(templates / "verify")
    rendered = cluster.render_tag(TAG, stubs, roles=roles)
    return {
        f"{TAG}-verify": [
            doc
            for name, text in sorted(rendered.verify.items())
            if name != "kustomization.yaml"
            for doc in yaml_fast.safe_load_all(text)
            if doc
        ]
    }


def test_the_tree_renders_no_tdarr_verify_while_ansible_owns_tdarr(stubs):
    assert cluster.render_tag(TAG, stubs).verify == {}


def test_the_flipped_job_passes_every_job_census_row(flipped):
    kinds = sorted(doc["kind"] for doc in flipped[f"{TAG}-verify"])
    assert kinds == ["ConfigMap", "Job", "ServiceAccount"]
    assert problems(flipped) == []


def test_the_census_fails_the_flipped_job_once_its_spec_moves(flipped):
    """The red case for the name row: an edited spec under the rendered name is refused."""
    docs = [dict(doc) for doc in flipped[f"{TAG}-verify"]]
    job = next(doc for doc in docs if doc["kind"] == "Job")
    job["spec"] = {**job["spec"], "backoffLimit": 1}
    found = problems({f"{TAG}-verify": docs})
    assert any(line.startswith("name: ") for line in found)
    assert any(line.startswith("backoff: ") for line in found)


def test_the_flipped_job_runs_as_tdarr_with_its_device_and_library(flipped):
    job = next(doc for doc in flipped[f"{TAG}-verify"] if doc["kind"] == "Job")
    pod = job["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    assert pod["securityContext"]["runAsUser"] == 1000
    assert pod["securityContext"]["supplementalGroups"] == [993]
    assert container["resources"]["limits"]["devic.es/dri"] == "1"
    media = next(m for m in container["volumeMounts"] if m["name"] == "media")
    assert (media["mountPath"], media["subPath"]) == ("/media", "media")
    assert pod["automountServiceAccountToken"] is False
    flux = yaml_fast.safe_load((K8S_ROLES / "flux/defaults/main.yml").read_text())
    assert container["image"] == flux["flux_job_image"]


def test_the_staged_job_reads_no_other_roles_file():
    """The deployer redeploys a role when a file it `lookup()`s in another role changes.

    The Job takes `flux_job_image` from the renderer rather than from the flux defaults, so a
    Renovate bump of that pin does not redeploy tdarr. The scan reads `verify-inert/` too.
    """
    readers = k8s_lookup_map(REPO)
    assert readers, "the scan found no cross-role lookup at all"
    assert [path for path, roles in readers.items() if TAG in roles] == []
