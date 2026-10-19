"""pihole's sibling guard moved into the role-owned kind that applies before `pihole` (#5719).

The guard landed inert in `templates/guard-inert/`. These tests copy the role, move that
directory to `templates/precheck/` as the flip PR would, and render the copy, so they check
that the guard fits the kind: it renders into `pihole-precheck`, meets every Job census row,
and holds `pihole` for longer than its own deadline. The guard's script and its naming are
covered in test_pihole_sibling_guard.py.

Run: uv run pytest ansible/roles/k8s/pihole/tests/test_pihole_precheck.py
"""

import re
import shutil

import pytest

from lib import yaml_fast
from lib.k8s_roles import K8S_ROLES
from lib.repo_paths import ANSIBLE
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.bundle import bundle_files
from renderer.renderer_lib.context import load_stubs
from renderer.tests._job_census import problems

TAG = "pihole"
DOTENV = "peanut_password=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n"


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    return load_stubs(path)


def _guard_docs(files: dict[str, str]) -> list[dict]:
    """Every document the directory's manifests hold, its `kustomization.yaml` aside."""
    return [
        doc
        for name, text in sorted(files.items())
        if name != cluster.KUSTOMIZATION
        for doc in yaml_fast.safe_load_all(text)
        if doc
    ]


def _jobs(files: dict[str, str]) -> list[dict]:
    return [doc for doc in _guard_docs(files) if doc["kind"] == "Job"]


def _seconds(duration: str) -> int:
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", duration)
    assert match, f"{duration!r} is not a duration of h, m and s"
    hours, minutes, secs = match.groups()
    return int(hours or 0) * 3600 + int(minutes or 0) * 60 + int(secs or 0)


PRECHECK = f"{TAG}-precheck"


@pytest.fixture(scope="module")
def precheck_tag(stubs, tmp_path_factory) -> cluster.RenderedTag:
    """The render of a role copy whose `guard-inert/` has moved to `templates/precheck/` (#5719)."""
    tree = tmp_path_factory.mktemp("precheck") / "ansible"
    role = tree / "roles" / "k8s" / TAG
    shutil.copytree(K8S_ROLES / TAG, role)
    (tree / "vars").mkdir()
    shutil.copy(ANSIBLE / "vars" / "cluster-vars.source", tree / "vars")
    (role / "templates" / "guard-inert").rename(role / "templates" / "precheck")
    copied = {**stubs, "playbook_dir": str(tree)}
    return cluster.render_tag(TAG, copied, roles=tree / "roles" / "k8s")


def test_the_guard_moved_into_the_precheck_kind_passes_every_job_census_row(
    precheck_tag,
):
    """In `pihole-precheck` the guard meets every row, `kind` included."""
    directories = precheck_tag.directories()
    assert PRECHECK in directories, f"the role copy renders no {PRECHECK}"
    docs = _guard_docs(directories[PRECHECK])
    (job,) = [doc for doc in docs if doc["kind"] == "Job"]
    assert job["metadata"]["name"].startswith("pihole-sibling-guard-")
    assert problems({PRECHECK: docs}) == []
    # Nothing of the guard stays in the Kustomizations that apply after instance 1 rolls.
    assert [
        name
        for name, files in directories.items()
        if name != PRECHECK
        and any("pihole-sibling-guard" in text for text in files.values())
    ] == []


def test_pihole_waits_on_the_guard_for_longer_than_its_deadline(precheck_tag):
    """A failing guard holds instance 1: `pihole` depends on `pihole-precheck`."""
    directories = precheck_tag.directories()
    assert PRECHECK in directories, f"the role copy renders no {PRECHECK}"
    files = bundle_files(directories, {}, [TAG], "300s", DOTENV)
    docs = {
        doc["metadata"]["name"]: doc
        for doc in yaml_fast.safe_load_all(files[f"flux/{TAG}.yaml"])
    }
    assert PRECHECK in docs, f"flux/{TAG}.yaml holds no {PRECHECK}"
    assert {"name": PRECHECK} in docs[TAG]["spec"]["dependsOn"]
    assert {"name": TAG} in docs[f"{TAG}-deferred"]["spec"]["dependsOn"]
    (job,) = _jobs(directories[PRECHECK])
    deadline = job["spec"]["activeDeadlineSeconds"]
    assert _seconds(docs[PRECHECK]["spec"]["timeout"]) > deadline
