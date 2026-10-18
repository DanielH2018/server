"""A snapshot role's `<tag>-snapshot`: its Job, its edges and its names (D5.2a).

The fixture renders speedtest, a real role that declares one snapshot claim, through the real
CLI as if the inventory marked it `owner: flux`, with a fixture ordering that gives it one
flux-owned upstream, so the cases hold whatever the live inventory publishes. code-server is the
first live tag that renders `<tag>-snapshot`, since D5.16. The plan is
docs/target-architecture/deploy/d5-bulk-move.md, step 11.

Run: uv run pytest scripts/renderer/tests/test_renderer_snapshot.py
"""

import json

import pytest
from _job_census import load, problems

from deploy_tools.bundle_census import report
from lib import yaml_fast
from lib.exit_codes import OK, RENDER_OK
from lib.k8s_roles import snapshot_claims
from lib.repo_paths import K8S_ROLES
from renderer import homelab_render
from renderer.renderer_lib.bundle import CLUSTER_VARS_FILE
from renderer.renderer_lib.environment import RenderError
from renderer.renderer_lib.ordering import Ordering
from renderer.renderer_lib.snapshot import (
    NAME_LIMIT,
    claim_namespaces,
    snapshot_files,
    snapshot_name,
)

DOTENV = "peanut_password=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n"
TAG = "speedtest"
UPSTREAM = "bento-pdf"


def _render(tmp_path, owned: set[str]):
    cluster_file = tmp_path / CLUSTER_VARS_FILE
    cluster_file.write_text(DOTENV)
    out = tmp_path / "out"
    code = homelab_render.cluster(
        out,
        [TAG],
        None,
        flux_owned=lambda: owned,
        cluster_file=cluster_file,
        ordering=lambda o: Ordering(deps={TAG: [UPSTREAM]}, flux_owned=frozenset(o)),
    )
    assert code == RENDER_OK
    return out


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    return _render(tmp_path_factory.mktemp("snapshot"), {TAG, UPSTREAM})


def _kustomizations(out) -> dict[str, dict]:
    docs = yaml_fast.safe_load_all((out / "flux" / f"{TAG}.yaml").read_text())
    return {doc["metadata"]["name"]: doc for doc in docs}


def _job(out) -> dict:
    return yaml_fast.safe_load(
        (out / "cluster" / f"{TAG}-snapshot" / "job.yaml").read_text()
    )


def _env(job: dict) -> dict[str, str]:
    container = job["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e["value"] for e in container["env"]}


def test_a_snapshot_role_gets_snapshot_kustomization_with_its_upstream_edge(rendered):
    docs = _kustomizations(rendered)
    assert list(docs) == [TAG, f"{TAG}-snapshot"]
    snapshot = docs[f"{TAG}-snapshot"]["spec"]
    assert [e["name"] for e in snapshot["dependsOn"]] == [UPSTREAM]
    # speedtest's own directory substitutes, so `<tag>` also waits on `cluster-vars`.
    assert [e["name"] for e in docs[TAG]["spec"]["dependsOn"]] == [
        UPSTREAM,
        "cluster-vars",
        f"{TAG}-snapshot",
    ]
    assert snapshot["wait"] is True and snapshot["deletionPolicy"] == "Orphan"


def test_the_job_embeds_the_digest8_that_digests_json_records(rendered):
    own = json.loads((rendered / "digests.json").read_text())[TAG]["own"]
    env = _env(_job(rendered))
    assert env["SNAPSHOT_DIGEST8"] == own[:8]
    assert json.loads(env["SNAPSHOT_CLAIMS"]) == [
        {"name": "speedtest-config", "namespace": "homelab"}
    ]


def test_the_snapshot_directory_passes_the_job_census_and_check_bundle(rendered):
    assert problems(load(rendered)) == []
    # `promote.py check-bundle`'s whole report: the census and the source, host and no-stub
    # gates, over a bundle holding the snapshot Job, its ClusterRole and its Roles.
    assert report(rendered, [TAG]) == OK


def test_an_unpublished_snapshot_role_renders_no_snapshot_directory(tmp_path):
    out = _render(tmp_path, set())
    assert sorted(p.name for p in (out / "cluster").iterdir()) == [TAG]


def _files(digest8: str = "0a1b2c3d", script: str = "print('a')\n", **claims) -> dict:
    return snapshot_files(
        TAG,
        digest8,
        claims or {"speedtest-config": "homelab"},
        "python:3.14-alpine",
        3,
        120,
        2,
        540,
        script,
        "helper\n",
    )


def _job_name(files: dict) -> str:
    return yaml_fast.safe_load(files["job.yaml"])["metadata"]["name"]


def test_the_job_name_moves_with_the_role_digest_and_with_the_script():
    name = _job_name(_files())
    assert name.startswith(f"snapshot-{TAG}-") and len(name.encode()) <= NAME_LIMIT
    assert _job_name(_files(digest8="ffffffff")) != name
    assert _job_name(_files(script="print('b')\n")) != name
    assert _job_name(_files()) == name


def test_every_snapshot_roles_longest_name_stays_within_63_bytes():
    claims, unjudged = snapshot_claims(
        sorted(p.parent.parent.name for p in K8S_ROLES.glob("*/defaults/main.yml"))
    )
    assert unjudged == []
    names = [snapshot_name(t, "0" * 8, c) for t, held in claims.items() for c in held]
    assert len(names) >= 14, (
        "the census found fewer snapshot claims than the fleet holds"
    )
    assert max(len(n.encode()) for n in names) <= NAME_LIMIT


def test_a_snapshot_name_over_63_bytes_fails_the_render():
    with pytest.raises(RenderError, match="over Longhorn's 63-byte delete limit"):
        _files(**{"speedtest-" + "x" * 30: "homelab"})


def test_a_snapshot_claim_the_tag_renders_no_pvc_for_fails_the_render():
    claim = "kind: PersistentVolumeClaim\nmetadata:\n  name: speedtest-config\n  namespace: homelab\n"
    assert claim_namespaces(TAG, {"c.yaml": claim}, ["speedtest-config"]) == {
        "speedtest-config": "homelab"
    }
    with pytest.raises(RenderError, match=r"\['speedtest-data'\]"):
        claim_namespaces(TAG, {"c.yaml": claim}, ["speedtest-data"])
