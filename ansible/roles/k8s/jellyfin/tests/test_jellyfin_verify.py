"""jellyfin's two Jobs: the verify script fails each broken check, and both pass the census.

The script tests run `jellyfin_verify.main` against a temporary device and library. The render
tests render a copy of the jellyfin role, so a test can edit the copy and leave the tree alone.
The plan is docs/target-architecture/deploy/d5-bulk-move.md.
The cap script's own tests are in test_jellyfin_snapshot_cap.py.

Run: uv run pytest ansible/roles/k8s/jellyfin/tests/test_jellyfin_verify.py
"""

import re
import shutil

import pytest

import jellyfin_verify
from lib import yaml_fast
from lib.k8s_roles import K8S_ROLES
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs
from renderer.tests._job_census import problems

TAG = "jellyfin"
# A path into a role's tree, as any lookup names it; group 1 is the role.
ROLE_PATH = re.compile(r"/roles/k8s/([^/'\"]+)/")


def _library(tmp_path, roots=("tv", "movies", "media/tv")):
    data = tmp_path / "data"
    for root in roots:
        (data / root).mkdir(parents=True)
    if "tv" in roots:
        (data / "tv" / "Show" / "Season 01").mkdir(parents=True)
        (data / "tv" / "Show" / "Season 01" / "e01.mkv").write_bytes(b"\x1a" * 2048)
    return data


def _env(tmp_path, device=None, data=None) -> dict[str, str]:
    if device is None:
        device = tmp_path / "renderD128"
        device.write_text("")
    if data is None:
        data = _library(tmp_path)
    return {"VERIFY_DEVICE": str(device), "VERIFY_DATA": str(data)}


def test_a_present_device_and_a_library_at_the_right_depth_pass(tmp_path, capsys):
    assert jellyfin_verify.main([], _env(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "renderD128 opens" in out
    assert "read 1024 bytes of" in out
    assert "e01.mkv" in out


def test_a_missing_render_node_fails_the_job(tmp_path, capsys):
    assert jellyfin_verify.main([], _env(tmp_path, device=tmp_path / "absent")) == 1
    assert "cannot open the render node" in capsys.readouterr().err


def test_a_missing_library_root_fails_the_job(tmp_path, capsys):
    data = _library(tmp_path, roots=("tv", "movies"))
    assert jellyfin_verify.main([], _env(tmp_path, data=data)) == 1
    assert "the library roots ['media/tv'] are not under" in capsys.readouterr().err


def test_the_volume_root_mounted_in_place_of_media_fails_the_job(tmp_path, capsys):
    """The B5 mistake: the roots exist one level down, and `torrents` sits beside them."""
    data = _library(tmp_path)
    (data / "torrents").mkdir()
    assert jellyfin_verify.main([], _env(tmp_path, data=data)) == 1
    assert "mounts the volume root" in capsys.readouterr().err


def test_a_library_with_no_file_to_read_fails_the_job(tmp_path, capsys):
    data = tmp_path / "data"
    for root in ("tv", "movies", "media/tv"):
        (data / root).mkdir(parents=True)
    assert jellyfin_verify.main([], _env(tmp_path, data=data)) == 1
    assert "no .mkv file under" in capsys.readouterr().err


def test_an_unreadable_library_file_fails_the_job(tmp_path, capsys):
    data = _library(tmp_path)
    sample = data / "tv" / "Show" / "Season 01" / "e01.mkv"
    sample.unlink()
    # A dangling symlink: the walk lists it, and opening it raises as an IO error would.
    sample.symlink_to(tmp_path / "gone.mkv")
    assert jellyfin_verify.main([], _env(tmp_path, data=data)) == 1
    assert "cannot read" in capsys.readouterr().err


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    return load_stubs(path)


def _flip(stubs, roles_dir, **overrides) -> dict[str, list[dict]]:
    """The `jellyfin-verify` documents a copy of the role renders."""
    shutil.copytree(K8S_ROLES / TAG, roles_dir / TAG)
    rendered = cluster.render_tag(TAG, {**stubs, **overrides}, roles=roles_dir)
    return {
        f"{TAG}-verify": [
            doc
            for name, text in sorted(rendered.verify.items())
            if name != "kustomization.yaml"
            for doc in yaml_fast.safe_load_all(text)
            if doc
        ]
    }


@pytest.fixture(scope="module")
def flipped(stubs, tmp_path_factory) -> dict[str, list[dict]]:
    return _flip(stubs, tmp_path_factory.mktemp("roles"))


def _job(flipped, prefix: str) -> dict:
    (job,) = [
        doc
        for doc in flipped[f"{TAG}-verify"]
        if doc["kind"] == "Job" and doc["metadata"]["name"].startswith(prefix)
    ]
    return job


def test_the_tree_renders_jellyfin_verify(stubs, flipped):
    """The flip moved both Jobs to `templates/verify/`, so the tree's own render emits them."""
    rendered = cluster.render_tag(TAG, stubs).verify
    assert f"{TAG}-verify" in flipped
    assert {"job.yaml", "cap.yaml"} <= {name.removesuffix(".j2") for name in rendered}


def test_the_flipped_jobs_pass_every_job_census_row(flipped):
    kinds = sorted(doc["kind"] for doc in flipped[f"{TAG}-verify"])
    assert kinds == [
        "ConfigMap",
        "ConfigMap",
        "Job",
        "Job",
        "Role",
        "Role",
        "RoleBinding",
        "RoleBinding",
        "ServiceAccount",
        "ServiceAccount",
    ]
    assert problems(flipped) == []


def test_the_census_fails_each_flipped_job_once_its_spec_moves(flipped):
    """The red case for the name row: an edited spec under the rendered name is refused."""
    for prefix in ("jellyfin-verify-", "jellyfin-snapshot-cap-"):
        docs = [dict(doc) for doc in flipped[f"{TAG}-verify"]]
        job = next(
            doc
            for doc in docs
            if doc["kind"] == "Job" and doc["metadata"]["name"].startswith(prefix)
        )
        job["spec"] = {**job["spec"], "activeDeadlineSeconds": 299}
        found = problems({f"{TAG}-verify": docs})
        assert [line for line in found if line.startswith("name: ")], prefix


def test_the_census_fails_the_cap_job_without_its_rerun_safe_test(flipped):
    docs = [dict(doc) for doc in flipped[f"{TAG}-verify"]]
    job = next(
        d
        for d in docs
        if d["kind"] == "Job"
        and d["metadata"]["name"].startswith("jellyfin-snapshot-cap-")
    )
    job["metadata"] = {
        **job["metadata"],
        "annotations": {"homelab/job-kind": "mutation"},
    }
    assert any(
        line.startswith("rerun-safe: ") for line in problems({f"{TAG}-verify": docs})
    )


def test_the_verify_job_runs_as_jellyfin_with_its_device_and_both_library_mounts(
    flipped,
):
    pod = _job(flipped, "jellyfin-verify-")["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    assert pod["securityContext"]["runAsUser"] == 1000
    assert pod["securityContext"]["supplementalGroups"] == [993]
    assert container["resources"]["limits"]["devic.es/dri"] == "1"
    media = [
        (m["mountPath"], m["subPath"], m["readOnly"])
        for m in container["volumeMounts"]
        if m["name"] == "media"
    ]
    assert media == [("/data", "media", True), ("/data/media", "media", True)]
    assert pod["automountServiceAccountToken"] is False
    flux = yaml_fast.safe_load((K8S_ROLES / "flux/defaults/main.yml").read_text())
    assert container["image"] == flux["flux_job_image"]


def test_the_cap_job_carries_the_cap_as_longhorn_stores_it(flipped):
    """Named member: 2 x 8Gi in bytes, a string, so the derived default was evaluated."""
    pod = _job(flipped, "jellyfin-snapshot-cap-")["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["CAP_MAX_SIZE"] == "17179869184"
    assert (env["CAP_NAMESPACE"], env["CAP_CLAIM"]) == ("homelab", "jellyfin-config")
    assert pod["serviceAccountName"] == "jellyfin-snapshot-cap"


def test_only_the_cap_account_holds_a_role(flipped):
    """The verify Job's account stays tokenless and ungranted; the patch grant is the cap's."""
    subjects = {
        s["name"]
        for doc in flipped[f"{TAG}-verify"]
        if doc["kind"] == "RoleBinding"
        for s in doc["subjects"]
    }
    assert subjects == {"jellyfin-snapshot-cap"}


def test_the_jobs_read_no_other_roles_file():
    """The deployer redeploys a role when a file it `lookup()`s in another role changes.

    The Jobs take `flux_job_image` and `flux_job_connect` from the renderer, so neither a bump
    of that pin nor an edit to the helper redeploys jellyfin.
    """
    staged = sorted((K8S_ROLES / TAG / "templates" / "verify").glob("*.j2"))
    assert [p.name for p in staged] == ["cap.yaml.j2", "job.yaml.j2", "rbac.yaml.j2"]
    assert [
        (path.name, role)
        for path in staged
        for role in ROLE_PATH.findall(path.read_text())
        if role != TAG
    ] == []


def test_a_deployment_change_renames_both_jobs(stubs, tmp_path):
    """A new jellyfin image changes the Deployment, so Flux runs both renamed Jobs again."""
    images = (
        "lscr.io/linuxserver/jellyfin:10.11.11",
        "lscr.io/linuxserver/jellyfin:10.11.12",
    )
    names = [
        sorted(
            doc["metadata"]["name"]
            for doc in _flip(stubs, tmp_path / str(i), jellyfin_k8s_image=image)[
                f"{TAG}-verify"
            ]
            if doc["kind"] == "Job"
        )
        for i, image in enumerate(images)
    ]
    assert len(names[0]) == 2
    assert set(names[0]).isdisjoint(names[1])
