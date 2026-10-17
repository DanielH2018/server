"""D5.3b2's `soak` kind: its footprint ignores R's SHA, its W comes from the rules, its Job wiring.

The plan is docs/target-architecture/deploy/d5-bulk-move.md, *The soak as a Job (D5.3)*.
Its suspend annotation is tested in test_renderer_suspend.py, and its Job in the Job census.

Run: uv run pytest scripts/renderer/tests/test_renderer_soak.py
"""

import json
import re

import pytest
import yaml

from changed_set import changed_set
from lib import yaml_fast
from lib.git import git_stdout
from lib.repo_paths import K8S_ROLES, REPO
from renderer.renderer_lib import soak
from renderer.renderer_lib.bundle import DIGESTS, bundle_files, digests

TIMEOUT = "600s"
DOTENV = "peanut_password=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n"
DIRECTORIES = {
    "bento-pdf": {
        "deployment.yaml": "kind: Deployment\nmetadata:\n  name: bento-pdf\n"
        "  namespace: homelab\n"
    },
    "sonarr": {"deployment.yaml": "kind: Deployment\nmetadata:\n  name: sonarr\n"},
    "sonarr-deferred": {
        "ns.yaml": "kind: Namespace\nmetadata:\n  name: media\n",
    },
}
TAGS = ["bento-pdf", "sonarr"]
R1, R2 = "1" * 40, "2" * 40


def _bundle(revision: str, roles) -> dict[str, str]:
    return bundle_files(
        DIRECTORIES, {}, TAGS, TIMEOUT, DOTENV, roles=roles, revision=revision
    )


def _doc(files: dict[str, str], path: str) -> dict:
    return yaml_fast.safe_load(files[path])


# -- the footprint ---------------------------------------------------------------------------


def test_two_commits_that_differ_only_in_docs_give_an_empty_changed_set(tmp_path):
    """A docs-only commit renders the same tags at a new SHA; `soak` must not move."""
    old, new = _bundle(R1, tmp_path), _bundle(R2, tmp_path)
    assert _doc(old, "cluster/soak/job.yaml") != _doc(new, "cluster/soak/job.yaml")
    found = changed_set(
        json.loads(new[DIGESTS]), json.loads(old[DIGESTS]), {"soak": []}
    )
    assert found.names == frozenset() and not found.widened


def test_a_digest_over_the_raw_render_puts_soak_in_the_set(tmp_path):
    """The red half: hashing the Job as published moves `soak` on every commit."""

    def raw(files: dict[str, str]) -> dict:
        cluster = {
            f"cluster/{name}/{f}": t
            for name, held in DIRECTORIES.items()
            for f, t in held.items()
        }
        everything = {p: t.encode() for p, t in {**cluster, **files}.items()}
        return digests(everything, TAGS)

    old, new = raw(_bundle(R1, tmp_path)), raw(_bundle(R2, tmp_path))
    assert changed_set(new, old, {"soak": []}).names == {"soak"}


# -- W, from the rules -----------------------------------------------------------------------


def _rules(tmp_path, *rules: dict):
    directory = tmp_path / "rules"
    directory.mkdir()
    (directory / "fixture.yaml").write_text(
        yaml.safe_dump({"groups": [{"name": "g", "rules": list(rules)}]})
    )
    return directory


def _alert(name: str, wait: str | None, **labels: str) -> dict:
    rule = {"alert": name, "expr": "vector(1)", "labels": labels}
    if wait:
        rule["for"] = wait
    return rule


def test_a_one_hour_critical_rule_lengthens_only_its_services_entry(tmp_path):
    rules = _rules(
        tmp_path,
        _alert("Long", "1h", severity="critical", service="sonarr"),
        _alert("Verified", "20m", severity="warning", verify="true", service="sonarr"),
        _alert("Short", "5m", severity="critical", service="bento-pdf"),
    )
    services = soak.service_map(TAGS, DIRECTORIES, soak.longest_for(rules))
    assert {tag: entry["for"] for tag, entry in services.items()} == {
        "bento-pdf": 300,
        "sonarr": 3600,
    }
    assert soak.fleet_wait(services, 60) == 3600 + soak.SCRAPE_AND_EVAL_SECONDS


def test_shadow_warning_and_watchdog_rules_add_nothing(tmp_path):
    rules = _rules(
        tmp_path,
        _alert("Shadow", "1h", severity="critical", shadow="true", service="sonarr"),
        _alert("Warning", "1h", severity="warning", service="sonarr"),
        _alert("Watchdog", "1h", severity="critical", service="sonarr"),
    )
    assert soak.longest_for(rules) == {}


def test_a_rule_with_no_static_service_counts_for_every_tag(tmp_path):
    rules = _rules(
        tmp_path,
        _alert("Any", "30m", severity="critical"),
        _alert("Templated", "40m", severity="critical", service="{{ $labels.x }}"),
    )
    services = soak.service_map(TAGS, DIRECTORIES, soak.longest_for(rules))
    assert {entry["for"] for entry in services.values()} == {2400}


def test_an_unreadable_for_fails_the_render(tmp_path):
    rules = _rules(tmp_path, _alert("Odd", "500ms", severity="critical"))
    with pytest.raises(ValueError, match="500ms"):
        soak.longest_for(rules)


def test_the_map_names_each_tags_namespaces_its_deferred_ones_included():
    services = soak.service_map(TAGS, DIRECTORIES, {})
    assert services["bento-pdf"]["namespaces"] == ["homelab"]
    assert services["sonarr"]["namespaces"] == ["media"]


def test_the_timeout_and_deadlines_follow_the_fleet_w(tmp_path):
    rules = _rules(
        tmp_path, _alert("Long", "1h", severity="critical", service="sonarr")
    )
    files = soak.soak_files(R1, TAGS, DIRECTORIES, rules=rules)
    wait = 3600 + soak.SCRAPE_AND_EVAL_SECONDS
    timeout = wait + soak.TIMEOUT_MARGIN_S
    assert _doc(files, "flux/soak.yaml")["spec"]["timeout"] == f"{timeout}s"
    job = _doc(files, "cluster/soak/job.yaml")
    deadline = timeout - soak.DEADLINE_MARGIN_S
    assert job["spec"]["activeDeadlineSeconds"] == deadline
    args = job["spec"]["template"]["spec"]["containers"][0]["args"]
    wait_seconds = int(args[args.index("--wait-seconds") + 1])
    assert 0 < wait_seconds < deadline - wait


# -- the Job's wiring ------------------------------------------------------------------------


def test_the_job_judges_r_from_a_pod_prometheus_admits(tmp_path):
    files = soak.soak_files(R1, TAGS, DIRECTORIES, rules=tmp_path)
    pod = _doc(files, "cluster/soak/job.yaml")["spec"]["template"]
    assert pod["metadata"]["labels"] == {"app": "soak"}
    container = pod["spec"]["containers"][0]
    assert container["name"] == "soak"
    assert container["args"][:2] == ["--revision", R1]
    services = json.loads(
        _doc(files, "cluster/soak/configmap.yaml")["data"]["services.json"]
    )
    assert set(services) == set(TAGS)


def test_the_rbac_reads_kustomizations_and_each_namespaces_pods_and_replicasets(
    tmp_path,
):
    files = soak.soak_files(R1, TAGS, DIRECTORIES, rules=tmp_path)
    roles = {
        doc["metadata"]["namespace"]: doc["rules"]
        for doc in yaml_fast.safe_load_all(files["cluster/soak/rbac.yaml"])
        if doc["kind"] == "Role"
    }
    assert set(roles) == {"flux-system", "homelab", "media"}
    granted = {
        (ns, resource, verb)
        for ns, rules in roles.items()
        for rule in rules
        for resource in rule["resources"]
        for verb in rule["verbs"]
    }
    assert {
        ("flux-system", "kustomizations", "get"),
        ("flux-system", "kustomizations", "list"),
        ("flux-system", "ocirepositories", "get"),
        ("media", "pods", "list"),
        ("media", "replicasets", "get"),
        ("homelab", "replicasets", "list"),
    } <= granted


# -- drift against what the Job reads --------------------------------------------------------


def test_scrape_seconds_matches_the_kube_state_metrics_job():
    text = (K8S_ROLES / "observability/templates/prometheus.yaml.j2").read_text()
    match = re.search(r"job_name: kube-state-metrics\n\s+scrape_interval: (\S+)", text)
    assert match, "the kube-state-metrics scrape job moved"
    assert soak.prometheus_seconds(match.group(1)) == soak.SCRAPE_SECONDS


def test_scrape_and_eval_seconds_matches_the_job_script():
    text = soak.SCRIPT.read_text()
    found = re.search(r"^SCRAPE_AND_EVAL_SECONDS = (\d+)$", text, re.MULTILINE)
    assert found and int(found.group(1)) == soak.SCRAPE_AND_EVAL_SECONDS


# -- R's SHA ---------------------------------------------------------------------------------


def test_the_revision_defaults_to_the_checkouts_head():
    head = git_stdout("rev-parse", "HEAD", cwd=REPO)
    assert soak.resolve_revision(None) == head
    assert soak.resolve_revision(R1) == R1


@pytest.mark.parametrize("given", ["nothex", "a" * 39, "A" * 40])
def test_a_revision_that_is_not_40_lowercase_hex_is_refused(given):
    with pytest.raises(ValueError, match="40-hex"):
        soak.resolve_revision(given)
