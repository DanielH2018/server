"""tdarr's inert verify Job: its script fails each broken check, and its render passes the census.

The script tests run `tdarr_verify.main` against a temporary device and library. The render
tests copy the tdarr role, move `templates/verify-inert/` to `templates/verify/` as the flip PR
will, and render the copy, so the Job is checked as Flux will receive it while the tree's own
render still emits no `tdarr-verify`. The plan is docs/target-architecture/deploy/d5-bulk-move.md,
*A verify conversion lands as inert code*.

Run: uv run pytest ansible/roles/k8s/tdarr/tests/test_tdarr_verify.py
"""

import re
import shutil

import pytest

import tdarr_verify
from lib import yaml_fast
from lib.k8s_roles import K8S_ROLES
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs
from renderer.tests._job_census import problems

TAG = "tdarr"
# A path into a role's tree, as any lookup names it; group 1 is the role.
ROLE_PATH = re.compile(r"/roles/k8s/([^/'\"]+)/")


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
    Renovate bump of that pin does not redeploy tdarr. The deployer's scan
    (`deploy_cross_role._K8S_LOOKUP_RE`) misses a lookup that passes keyword arguments, so
    this reads every role path the staged templates name, in any form.
    """
    staged = sorted((K8S_ROLES / TAG / "templates" / "verify-inert").glob("*.j2"))
    assert [p.name for p in staged] == ["job.yaml.j2", "rbac.yaml.j2"]
    assert [
        (path.name, role)
        for path in staged
        for role in ROLE_PATH.findall(path.read_text())
        if role != TAG
    ] == []


def test_the_role_path_scan_finds_a_lookup_with_keyword_arguments():
    """The red case: the form the deployer's regex misses is the form the scan must find."""
    text = "lookup('file', playbook_dir + '/roles/k8s/flux/defaults/main.yml', rstrip=False)"
    assert ROLE_PATH.findall(text) == ["flux"]


def test_a_deployment_change_renames_the_job(stubs, tmp_path):
    """A new tdarr image changes the Deployment, so Flux runs the renamed Job again."""
    names = []
    for image in (
        "ghcr.io/haveagitgat/tdarr:3.00.00",
        "ghcr.io/haveagitgat/tdarr:3.00.01",
    ):
        roles = tmp_path / image.rsplit(":", 1)[1]
        shutil.copytree(K8S_ROLES / TAG, roles / TAG)
        templates = roles / TAG / "templates"
        (templates / "verify-inert").rename(templates / "verify")
        rendered = cluster.render_tag(
            TAG, {**stubs, "tdarr_k8s_image": image}, roles=roles
        )
        docs = yaml_fast.safe_load_all(rendered.verify["job.yaml"])
        names.append(
            next(d for d in docs if d and d["kind"] == "Job")["metadata"]["name"]
        )
    assert names[0] != names[1]
