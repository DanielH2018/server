"""configarr's two gate Jobs for Flux: a re-run changes nothing, and the render passes the census.

`flux_cronjob_gates` in this role's defaults opts both CronJobs into a gate Job that the
renderer copies into `configarr-verify` (D5, *`cronjob-gate`'s gate Job*). The key is inert
while configarr is Ansible-owned, so the render tests render the real role as if the inventory
marked it `owner: flux`.

configarr's gate is a post-apply mutation, and its `homelab/rerun-safe` annotation names
`test_a_second_sync_reports_no_change`. That test reads the change counters configarr logged in
three consecutive live runs on 2026-10-17 and 2026-10-18, the 03:45 deploy gate run between two
scheduled 04:30 runs, all on the same image. Every one reported nothing created or updated.

Run: uv run pytest ansible/roles/k8s/configarr/tests/test_configarr_gate.py
"""

import re

import pytest

from lib import yaml_fast
from lib.exit_codes import RENDER_OK
from lib.k8s_roles import K8S_ROLES
from renderer import homelab_render
from renderer.renderer_lib.bundle import CLUSTER_VARS_FILE
from renderer.renderer_lib.cronjob_gate import GATE_INFIX, MAX_NAME, RERUN_SAFE
from renderer.renderer_lib.ordering import Ordering
from renderer.tests._job_census import load, problems

TAG = "configarr"
VERIFY = f"{TAG}-verify"
DOTENV = "peanut_password=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n"

# The counter lines of `kubectl -n homelab logs job/<name>` for each run, ANSI codes stripped.
LIVE_RUNS = {
    "configarr-29870490": """
INFO  [09:30:22.807]: Created CFs: 0, Updated CFs: 0, Untouched CFs: 35, Error CFs: 0
INFO  [09:30:22.827]: QualityProfiles: Create: 0, Update: 0, Unchanged: 8
INFO  [09:30:23.118]: Created CFs: 0, Updated CFs: 0, Untouched CFs: 34, Error CFs: 0
INFO  [09:30:23.135]: QualityProfiles: Create: 0, Update: 0, Unchanged: 7
""",
    "configarr-deploy-gate": """
INFO  [03:46:08.681]: Created CFs: 0, Updated CFs: 0, Untouched CFs: 35, Error CFs: 0
INFO  [03:46:08.717]: QualityProfiles: Create: 0, Update: 0, Unchanged: 8
INFO  [03:46:09.164]: Created CFs: 0, Updated CFs: 0, Untouched CFs: 34, Error CFs: 0
INFO  [03:46:09.179]: QualityProfiles: Create: 0, Update: 0, Unchanged: 7
""",
    "configarr-29871930": """
INFO  [09:30:34.270]: Created CFs: 0, Updated CFs: 0, Untouched CFs: 35, Error CFs: 0
INFO  [09:30:34.285]: QualityProfiles: Create: 0, Update: 0, Unchanged: 8
INFO  [09:30:34.566]: Created CFs: 0, Updated CFs: 0, Untouched CFs: 34, Error CFs: 0
INFO  [09:30:34.579]: QualityProfiles: Create: 0, Update: 0, Unchanged: 7
""",
}
# One line per *arr instance for each counter, so a log missing an instance is not a clean run.
INSTANCES = 2
_CHANGES = re.compile(
    r"Created CFs: (\d+), Updated CFs: (\d+)|QualityProfiles: Create: (\d+), Update: (\d+)"
)


def changes(log: str) -> list[int]:
    """Each create or update count configarr logged, in order."""
    return [int(n) for match in _CHANGES.finditer(log) for n in match.groups() if n]


def test_a_second_sync_reports_no_change():
    # fact: ansible/roles/k8s/configarr/CLAUDE.md#At a glance
    for name, log in LIVE_RUNS.items():
        counts = changes(log)
        assert len(counts) == 4 * INSTANCES, name
        assert not any(counts), f"{name} changed the *arrs: {counts}"


def test_a_run_that_updated_a_custom_format_counts_as_a_change():
    log = LIVE_RUNS["configarr-deploy-gate"].replace(
        "Updated CFs: 0", "Updated CFs: 1", 1
    )
    assert changes(log) == [0, 1, 0, 0, 0, 0, 0, 0]


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    """The bundle configarr renders as a flux-owned tag."""
    tmp_path = tmp_path_factory.mktemp("configarr-gate")
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
        vendored=K8S_ROLES,
    )
    assert code == RENDER_OK
    return out


def _gate(out, cronjob: str) -> dict:
    [job] = [
        d
        for d in yaml_fast.safe_load_all(
            (out / "cluster" / VERIFY / f"{cronjob}-deploy-gate.yaml").read_text()
        )
        if d
    ]
    return job


def test_the_reconcile_gate_is_a_rerun_safe_mutation_the_health_reader_counts(rendered):
    job = _gate(rendered, "configarr")
    assert job["metadata"]["name"].startswith(f"configarr{GATE_INFIX}")
    assert job["metadata"]["annotations"] == {
        "homelab/job-kind": "mutation",
        RERUN_SAFE: "ansible/roles/k8s/configarr/tests/test_configarr_gate.py"
        "::test_a_second_sync_reports_no_change",
    }
    # configarr_health.py lists Jobs by `-l app=configarr` and picks the latest by finish time,
    # so the Job label, not the name, is what keeps a gate run counted.
    assert job["metadata"]["labels"] == {"app": "configarr"}
    assert job["spec"]["template"]["metadata"]["labels"]["app"] == "configarr"
    # The CronJob's own value, kept: a retried sync would report a transient outage as clean.
    assert job["spec"]["backoffLimit"] == 0


def test_the_health_gate_is_a_verify_outside_the_readers_selector(rendered):
    job = _gate(rendered, "configarr-health")
    assert job["metadata"]["annotations"] == {"homelab/job-kind": "verify"}
    assert job["spec"]["template"]["metadata"]["labels"]["app"] == "configarr-health"
    assert job["spec"]["backoffLimit"] == 0


def test_both_gates_pass_the_job_census(rendered):
    # The CronJobs' own directory comes too: a gate copy is exempt from the census's script
    # delivery rows only when its pod template equals a CronJob's there (`is_gate_copy`).
    loaded = load(rendered)
    bundle = {TAG: loaded[TAG], VERIFY: loaded[VERIFY]}
    names = sorted(
        d["metadata"]["name"] for d in bundle[VERIFY] if d.get("kind") == "Job"
    )
    assert [n.rsplit("-", 1)[0] for n in names] == [
        "configarr-deploy-gate",
        "configarr-health-deploy-gate",
    ]
    assert all(len(n) <= MAX_NAME for n in names)
    assert problems(bundle) == []
