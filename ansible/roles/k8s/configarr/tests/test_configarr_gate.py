"""configarr's two gate Jobs for Flux: a re-run changes nothing, and the render passes the census.

`flux_cronjob_gates` in this role's defaults opts both CronJobs into a gate Job that the
renderer copies into `configarr-verify` (D5, *`cronjob-gate`'s gate Job*). The key is inert
while configarr is Ansible-owned, so the render tests render the real role as if the inventory
marked it `owner: flux`.

configarr's gate is a post-apply mutation, and its `homelab/rerun-safe` annotation names
`test_a_second_sync_reports_no_change`. configarr overwrites each value it manages with the one
its config names, so that test reads the config the role renders and fails on the shapes that
would make a second run write something new (`nonconvergent`). The live evidence, three
consecutive runs on 2026-10-17 and 2026-10-18 that each reported nothing created or updated,
is quoted in the PR that added this module rather than frozen here.

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

# A full commit SHA: a branch or tag can move between the gate run and the next, so the second
# run would apply different templates from the first.
_PINNED = re.compile(r"[0-9a-f]{40}")


def nonconvergent(config: dict) -> list[str]:
    """Why a second configarr sync with `config` would write something the first did not.

    configarr overwrites each value it manages with the one its config names, so a second run
    is a no-op unless two entries write the same value differently, or the templates move under
    it. That leaves three shapes:

    - two `customFormatDefinitions` with the same `trash_id` or `name`, which configarr writes
      in turn onto one custom format;
    - one custom format scored twice in one profile of one instance, with different scores;
    - `recyclarrRevision` not pinned to a commit.

    Args:
        config: The parsed `config.yml` the `configarr-config` Secret carries.

    Returns:
        One line per problem, empty when a second run converges.
    """
    found = []
    for key in ("trash_id", "name"):
        values = [d.get(key) for d in config.get("customFormatDefinitions") or []]
        found += [
            f"customFormatDefinitions {key} {v!r} is defined twice"
            for v in sorted({v for v in values if values.count(v) > 1})
        ]
    for arr in ("sonarr", "radarr"):
        for instance, body in (config.get(arr) or {}).items():
            scores: dict[tuple[str, str], set] = {}
            for entry in body.get("custom_formats") or []:
                for trash_id in entry.get("trash_ids") or []:
                    for target in entry.get("assign_scores_to") or []:
                        key = (trash_id, target.get("name"))
                        scores.setdefault(key, set()).add(target.get("score"))
            found += [
                f"{arr}/{instance}: {tid} is scored {sorted(s, key=str)} in {profile}"
                for (tid, profile), s in sorted(scores.items())
                if len(s) > 1
            ]
    if not _PINNED.fullmatch(str(config.get("recyclarrRevision", ""))):
        found.append("recyclarrRevision is not pinned to a commit")
    return found


def _config(out) -> dict:
    """The `config.yml` of the rendered `configarr-config` Secret."""
    [secret] = [
        d
        for d in yaml_fast.safe_load_all(
            (out / "cluster" / TAG / "config-secret.yaml").read_text()
        )
        if d
    ]
    return yaml_fast.safe_load(secret["stringData"]["config.yml"])


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


def test_a_second_sync_reports_no_change(rendered):
    # fact: ansible/roles/k8s/configarr/CLAUDE.md#At a glance
    config = _config(rendered)
    # Non-vacuity: the render carries the four local definitions and both instances.
    assert len(config["customFormatDefinitions"]) == 4
    assert set(config["sonarr"]) == {"series"} and set(config["radarr"]) == {"movies"}
    assert nonconvergent(config) == []


def test_a_custom_format_defined_twice_is_flagged(rendered):
    config = _config(rendered)
    config["customFormatDefinitions"].append(dict(config["customFormatDefinitions"][0]))
    assert nonconvergent(config) == [
        "customFormatDefinitions trash_id 'local-fake-remux-groups' is defined twice",
        "customFormatDefinitions name 'Fake/Mislabeled Remux Groups' is defined twice",
    ]


def test_a_custom_format_scored_twice_in_one_profile_is_flagged(rendered):
    config = _config(rendered)
    config["sonarr"]["series"]["custom_formats"].append(
        {
            "trash_ids": ["local-anime-trusted-groups"],
            "assign_scores_to": [{"name": "Anime", "score": 50}],
        }
    )
    assert nonconvergent(config) == [
        "sonarr/series: local-anime-trusted-groups is scored [200, 50] in Anime"
    ]


def test_a_template_revision_on_a_branch_is_flagged(rendered):
    config = _config(rendered)
    config["recyclarrRevision"] = "master"
    assert nonconvergent(config) == ["recyclarrRevision is not pinned to a commit"]
