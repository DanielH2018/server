"""A CronJob's gate Job in `<tag>-verify`, copied from its `jobTemplate` (D5, #5556).

The fixture renders pi-peer-backup, the real role whose gate the plan converts first, through
the real CLI as if the inventory marked it `owner: flux`, from a copy of its role tree whose
defaults opt its CronJob in. No role opts in on master, so no live tag renders a gate Job.

Run: uv run pytest scripts/renderer/tests/test_renderer_cronjob_gate.py
"""

import fnmatch
import json
import re
import shutil

import pytest
from _job_census import load, problems

from deploy_tools.bundle_census import report
from lib import yaml_fast
from lib.exit_codes import OK, RENDER_OK
from lib.repo_paths import K8S_ROLES
from renderer import homelab_render
from renderer.renderer_lib.bundle import CLUSTER_VARS_FILE
from renderer.renderer_lib.cluster import RenderedTag
from renderer.renderer_lib.cronjob_gate import attach_gates, gate_job, gate_name
from renderer.renderer_lib.digest8 import job_spec_digest8
from renderer.renderer_lib.environment import RenderError
from renderer.renderer_lib.ordering import Ordering, cronjob_gates, verify_timeout

DOTENV = "peanut_password=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n"
TAG = "pi-peer-backup"
VERIFY = f"{TAG}-verify"
GATE_FILE = f"{TAG}-deploy-gate.yaml"
PULL_SCRIPT = K8S_ROLES / TAG / "files" / "pull-pi-peers.sh"
OPT_IN = f"\nflux_cronjob_gates:\n  {TAG}:\n    kind: verify\n"
# The census rows a gate Job copied from a CronJob meets. `delivery` and `helper` read the
# Job's command and scripts, which pi-peer-backup's image carries, so its flip PR owns them.
GATE_ROWS = ("kind", "ttl", "name", "backoff", "rerun-safe")


def _roles(tmp_path, defaults: str = OPT_IN):
    roles = tmp_path / "roles"
    shutil.copytree(K8S_ROLES / TAG, roles / TAG)
    with (roles / TAG / "defaults" / "main.yml").open("a") as handle:
        handle.write(defaults)
    return roles


def _render(tmp_path, roles):
    cluster_file = tmp_path / CLUSTER_VARS_FILE
    cluster_file.write_text(DOTENV)
    out = tmp_path / "out"
    code = homelab_render.cluster(
        out,
        [TAG],
        None,
        flux_owned=lambda: {TAG},
        cluster_file=cluster_file,
        ordering=lambda o: Ordering(flux_owned=frozenset(o)),
        vendored=roles,
    )
    assert code == RENDER_OK
    return out


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("gate")
    return _render(tmp_path, _roles(tmp_path))


def _docs(path) -> list[dict]:
    return [d for d in yaml_fast.safe_load_all(path.read_text()) if d]


def _gate(out) -> dict:
    [job] = _docs(out / "cluster" / VERIFY / GATE_FILE)
    return job


def _cronjob(out) -> dict:
    docs = [
        d
        for path in (out / "cluster" / TAG).glob("*.yaml")
        for d in _docs(path)
        if d.get("kind") == "CronJob"
    ]
    [cronjob] = docs
    return cronjob


def test_a_verify_gate_is_the_cronjobs_job_template_named_by_its_spec(rendered):
    job, cronjob = _gate(rendered), _cronjob(rendered)
    template = cronjob["spec"]["jobTemplate"]["spec"]["template"]
    assert job["metadata"]["name"] == gate_name(TAG, job_spec_digest8(job))
    assert job["metadata"]["namespace"] == cronjob["metadata"]["namespace"]
    assert job["metadata"]["annotations"] == {"homelab/job-kind": "verify"}
    assert "ttlSecondsAfterFinished" not in job["spec"]
    # The CronJob's own value is 1; a verify gate must fail rather than pass on a retry.
    assert job["spec"]["backoffLimit"] == 0
    assert (
        job["spec"]["template"]["metadata"]["labels"] == template["metadata"]["labels"]
    )
    assert job["spec"]["template"]["spec"] == template["spec"]


def test_the_gate_embeds_the_roles_digest8_so_each_revision_renames_it(rendered):
    own = json.loads((rendered / "digests.json").read_text())[TAG]["own"]
    annotations = _gate(rendered)["spec"]["template"]["metadata"]["annotations"]
    assert annotations["homelab/role-digest8"] == own[:8]


def test_the_verify_kustomization_outlasts_the_gates_deadline(rendered):
    docs = {d["metadata"]["name"]: d for d in _docs(rendered / "flux" / f"{TAG}.yaml")}
    deadline = _gate(rendered)["spec"]["activeDeadlineSeconds"]
    assert deadline == 600
    assert docs[VERIFY]["spec"]["timeout"] == "660s"
    assert docs[TAG]["spec"]["timeout"] == "600s"
    assert [e["name"] for e in docs[VERIFY]["spec"]["dependsOn"]] == [TAG]


def test_the_gate_passes_check_bundle_and_the_job_census_rows_it_owns(rendered):
    assert report(rendered, [TAG], rendered.parent / "roles") == OK
    bundle = {VERIFY: load(rendered)[VERIFY]}
    assert [p for p in problems(bundle) if p.split(":")[0] in GATE_ROWS] == []


def test_check_bundle_refuses_the_verify_directory_of_a_role_without_the_opt_in(
    rendered, capsys
):
    assert report(rendered, [TAG]) != OK
    assert "pi-peer-backup-verify" in capsys.readouterr().err


def _hostname_glob() -> str:
    """The `HOSTNAME` pattern `pull-pi-peers.sh` reads a gate run by, as an fnmatch glob."""
    match = re.search(
        r'\[\[ "\$\{HOSTNAME:-\}" == ([^ ]+) \]\] && GATE_RUN=1',
        PULL_SCRIPT.read_text(),
    )
    assert match, f"{PULL_SCRIPT} no longer reads a gate run from HOSTNAME"
    return match.group(1)


def test_pull_pi_peers_reads_the_gates_pods_as_a_gate_run(rendered):
    pod = f"{_gate(rendered)['metadata']['name']}-x7k2q"
    assert fnmatch.fnmatchcase(pod, _hostname_glob())


def test_pull_pi_peers_reads_a_gate_named_without_deploy_as_a_scheduled_run(rendered):
    digest8 = job_spec_digest8(_gate(rendered))
    assert not fnmatch.fnmatchcase(f"{TAG}-gate-{digest8}-x7k2q", _hostname_glob())


def test_a_role_without_the_opt_in_renders_no_verify_directory(tmp_path):
    out = _render(tmp_path, _roles(tmp_path, defaults=""))
    assert not (out / "cluster" / VERIFY).exists()


CRONJOB: dict = {
    "kind": "CronJob",
    "metadata": {"name": "sync", "namespace": "media"},
    "spec": {
        "jobTemplate": {
            "spec": {
                "backoffLimit": 2,
                "ttlSecondsAfterFinished": 86400,
                "template": {
                    "metadata": {"labels": {"app": "sync"}},
                    "spec": {"containers": [{"name": "sync", "image": "sync:1"}]},
                },
            }
        }
    },
}
RERUN = "ansible/roles/k8s/sync/tests/test_sync.py::test_a_second_run_changes_nothing"


def test_a_mutation_gate_keeps_its_backoff_and_names_its_rerun_test():
    job = gate_job(CRONJOB, "abcd1234", {"kind": "mutation", "rerun_safe": RERUN})
    assert job["spec"]["backoffLimit"] == 2
    assert job["metadata"]["annotations"] == {
        "homelab/job-kind": "mutation",
        "homelab/rerun-safe": RERUN,
    }
    assert "ttlSecondsAfterFinished" not in job["spec"]
    # The copy is a deep one: the CronJob Flux applies keeps its own TTL.
    assert CRONJOB["spec"]["jobTemplate"]["spec"]["ttlSecondsAfterFinished"] == 86400


def test_a_new_role_revision_renames_the_gate():
    first = gate_job(CRONJOB, "abcd1234", {"kind": "verify"})
    second = gate_job(CRONJOB, "abcd1235", {"kind": "verify"})
    assert first["metadata"]["name"] != second["metadata"]["name"]


@pytest.mark.parametrize(
    "gates",
    [
        {"sync": {"kind": "mutation"}},
        {"sync": {"kind": "verify", "rerun_safe": RERUN}},
        {"sync": {"kind": "probe"}},
        {"sync": "verify"},
    ],
    ids=["mutation-without-rerun-safe", "verify-with-rerun-safe", "no-kind", "bare"],
)
def test_a_malformed_opt_in_is_a_problem(tmp_path, gates):
    defaults = tmp_path / "sync" / "defaults" / "main.yml"
    defaults.parent.mkdir(parents=True)
    defaults.write_text(json.dumps({"flux_cronjob_gates": gates}))
    found, problems_ = cronjob_gates(["sync"], tmp_path)
    assert found == {}
    assert len(problems_) == 1 and problems_[0].startswith("sync's flux_cronjob_gates")


def test_an_opt_in_naming_no_rendered_cronjob_fails_the_render(tmp_path):
    roles = _roles(
        tmp_path, defaults="\nflux_cronjob_gates:\n  nightly:\n    kind: verify\n"
    )
    tag = RenderedTag(
        TAG, {"cronjob.yaml": "kind: CronJob\nmetadata:\n  name: other\n"}, {}
    )
    with pytest.raises(RenderError, match=r"names \['nightly'\]"):
        attach_gates([tag], [TAG], roles)


@pytest.mark.parametrize(
    ("deadline", "want"),
    [(None, "10m"), (540, "10m"), (541, "601s"), (3600, "3660s")],
)
def test_the_verify_timeout_rises_only_above_a_deadline_plus_the_margin(deadline, want):
    spec = "{}" if deadline is None else f"{{activeDeadlineSeconds: {deadline}}}"
    assert verify_timeout({"job.yaml": f"kind: Job\nspec: {spec}\n"}, "10m") == want
