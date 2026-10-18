"""media-volume's verify Job: its script fails a split volume, and its render passes the census.

The script tests run `media_volume_verify.main` against a temporary volume. The render tests
render a copy of the media-volume role, so one test can move the host path and leave the tree
alone. The plan is docs/target-architecture/deploy/d5-bulk-move.md.

Run: uv run pytest ansible/roles/k8s/media-volume/tests/test_media_volume_verify.py
"""

import os
import re
import shutil

import pytest

import media_volume_verify
from lib import yaml_fast
from lib.k8s_roles import K8S_ROLES
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs
from renderer.tests._job_census import problems

TAG = "media-volume"
# A path into a role's tree, as any lookup names it; group 1 is the role.
ROLE_PATH = re.compile(r"/roles/k8s/([^/'\"]+)/")


def _env(data) -> dict[str, str]:
    return {"VERIFY_DATA": str(data), "HOSTNAME": "media-volume-verify-abc"}


def _volume(tmp_path):
    data = tmp_path / "data"
    (data / "media" / "tv").mkdir(parents=True)
    (data / "torrents").mkdir()
    return data


def test_one_filesystem_passes_and_leaves_the_volume_as_found(tmp_path, capsys):
    data = _volume(tmp_path)
    os.utime(data / "media", ns=(1_000_000_000, 1_000_000_000))
    assert media_volume_verify.main([], _env(data)) == 0
    assert "are one filesystem" in capsys.readouterr().out
    assert sorted(p.name for p in (data / "media").iterdir()) == ["tv"]
    assert list((data / "torrents").iterdir()) == []
    assert os.stat(data / "media").st_mtime_ns == 1_000_000_000


def test_a_fresh_volume_gets_both_directories(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    assert media_volume_verify.main([], _env(data)) == 0
    assert sorted(p.name for p in data.iterdir()) == ["media", "torrents"]


def test_two_filesystems_fail_the_job(tmp_path, capsys):
    def cross_device(src, dst):
        raise OSError(media_volume_verify.EXDEV, "Invalid cross-device link")

    data = _volume(tmp_path)
    assert media_volume_verify.main([], _env(data), cross_device) == 1
    assert "are two filesystems" in capsys.readouterr().err
    assert list((data / "torrents").iterdir()) == []


def test_a_wrong_link_count_fails_the_job(tmp_path, capsys):
    """A copy instead of a link leaves each file with one link."""
    assert media_volume_verify.main([], _env(_volume(tmp_path)), shutil.copyfile) == 1
    assert "link count is 1, expected 2" in capsys.readouterr().err


def test_an_unmounted_volume_fails_the_job(tmp_path, capsys):
    assert media_volume_verify.main([], _env(tmp_path / "absent")) == 1
    assert "is not mounted" in capsys.readouterr().err


def test_a_read_only_volume_fails_the_job(tmp_path, capsys):
    data = _volume(tmp_path)
    (data / "torrents").chmod(0o555)
    try:
        assert media_volume_verify.main([], _env(data)) == 1
    finally:
        (data / "torrents").chmod(0o755)
    assert "is not writable" in capsys.readouterr().err


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    return load_stubs(path)


def _flipped_render(stubs, roles, overrides=None):
    shutil.copytree(K8S_ROLES / TAG, roles / TAG)
    return cluster.render_tag(TAG, {**stubs, **(overrides or {})}, roles=roles)


@pytest.fixture(scope="module")
def flipped(stubs, tmp_path_factory) -> dict[str, list[dict]]:
    """The `media-volume-verify` directory a copy of the role renders."""
    rendered = _flipped_render(stubs, tmp_path_factory.mktemp("roles"))
    return {
        f"{TAG}-verify": [
            doc
            for name, text in sorted(rendered.verify.items())
            if name != "kustomization.yaml"
            for doc in yaml_fast.safe_load_all(text)
            if doc
        ]
    }


def test_the_tree_renders_media_volume_verify(stubs, tmp_path):
    """The flip moved the Job to `templates/verify/`, so the tree's own render emits it."""
    own = cluster.render_tag(TAG, stubs).verify
    assert own == _flipped_render(stubs, tmp_path).verify != {}


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


def test_the_flipped_job_mounts_the_claim_as_the_media_stack(flipped):
    job = next(doc for doc in flipped[f"{TAG}-verify"] if doc["kind"] == "Job")
    pod = job["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    assert pod["securityContext"]["runAsUser"] == 1000
    assert pod["securityContext"]["runAsGroup"] == 1000
    assert "fsGroup" not in pod["securityContext"]
    media = next(v for v in pod["volumes"] if v["name"] == "media")
    assert media["persistentVolumeClaim"]["claimName"] == "media-data"
    mount = next(m for m in container["volumeMounts"] if m["name"] == "media")
    assert (mount["mountPath"], mount.get("subPath")) == ("/data", None)
    assert pod["automountServiceAccountToken"] is False
    flux = yaml_fast.safe_load((K8S_ROLES / "flux/defaults/main.yml").read_text())
    assert container["image"] == flux["flux_job_image"]


def test_the_staged_job_reads_no_other_roles_file():
    """The deployer redeploys a role when a file it `lookup()`s in another role changes."""
    staged = sorted((K8S_ROLES / TAG / "templates" / "verify").glob("*.j2"))
    assert [p.name for p in staged] == ["job.yaml.j2", "rbac.yaml.j2"]
    assert [
        (path.name, role)
        for path in staged
        for role in ROLE_PATH.findall(path.read_text())
        if role != TAG
    ] == []


def test_a_pv_change_renames_the_job(stubs, tmp_path):
    """A moved host path changes the PV, so Flux runs the renamed Job again."""
    names = []
    for path in ("/srv/media", "/srv/media2"):
        rendered = _flipped_render(
            stubs, tmp_path / path.strip("/"), {"media_volume_host_path": path}
        )
        docs = yaml_fast.safe_load_all(rendered.verify["job.yaml"])
        names.append(
            next(d for d in docs if d and d["kind"] == "Job")["metadata"]["name"]
        )
    assert names[0] != names[1]
