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
from deploy_tools.chained_timeout import promote_timeout
from renderer.renderer_lib.bundle import (
    DIGESTS,
    bundle_files,
    digests,
    tag_kustomization,
)

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


def test_the_shipped_rules_give_mosquitto_its_metric_verify():
    """D5's verify-on-a-metric conversion: MosquittoLanAddressNotPinned, a 5m warning."""
    assert soak.longest_for().get("mosquitto") == 300


def test_the_soak_verifies_dri_device_plugin_through_its_unshadowed_rule(tmp_path):
    """K8sExtendedResourceMissing carries `verify` and no `shadow`, so the census counts its 5m.

    The census skips a shadow rule, as the soak's alert filter does, so restoring the label on
    a copy of the rules takes dri-device-plugin out again.
    """
    shipped = soak.RULES / "k8s-workloads.yaml"
    groups = yaml_fast.safe_load(shipped.read_text())["groups"]
    rule = next(
        r
        for g in groups
        for r in g["rules"]
        if r.get("alert") == "K8sExtendedResourceMissing"
    )
    assert rule["labels"].get("verify") == "true"
    assert "shadow" not in rule["labels"]
    assert soak.longest_for()["dri-device-plugin"] == 300
    rule["labels"]["shadow"] = "true"
    (tmp_path / "k8s-workloads.yaml").write_text(yaml.safe_dump({"groups": groups}))
    assert "dri-device-plugin" not in soak.longest_for(tmp_path)


def test_the_map_names_each_tags_namespaces_its_deferred_ones_included():
    services = soak.service_map(TAGS, DIRECTORIES, {})
    assert services["bento-pdf"]["namespaces"] == ["homelab"]
    assert services["sonarr"]["namespaces"] == ["media"]


def _ks(name: str, timeout: str, *depends: str) -> dict:
    doc = tag_kustomization(name, False, timeout)
    if depends:
        doc["spec"]["dependsOn"] = [{"name": d} for d in depends]
    return doc


def _budget(files: dict[str, str]) -> tuple[int, int, int]:
    """The Job's Kustomization wait, its deadline and the Kustomization's timeout."""
    job = _doc(files, "cluster/soak/job.yaml")
    args = job["spec"]["template"]["spec"]["containers"][0]["args"]
    timeout = _doc(files, "flux/soak.yaml")["spec"]["timeout"]
    return (
        int(args[args.index("--wait-seconds") + 1]),
        job["spec"]["activeDeadlineSeconds"],
        int(timeout.removesuffix("s")),
    )


def test_the_wait_covers_a_slow_tags_own_timeout_and_w_follows(tmp_path):
    """The soak starts beside the tags, so a 50-minute tag must fit inside its wait."""
    rules = _rules(
        tmp_path, _alert("Long", "1h", severity="critical", service="sonarr")
    )
    others = [_ks("bento-pdf", "10m"), _ks("sonarr", "50m")]
    files = soak.soak_files(R1, TAGS, DIRECTORIES, rules=rules, kustomizations=others)
    wait, deadline, timeout = _budget(files)
    assert wait == promote_timeout(others) and wait > 50 * 60
    w = 3600 + soak.SCRAPE_AND_EVAL_SECONDS
    assert deadline == wait + w + soak.DEADLINE_MARGIN_S
    assert timeout == deadline + soak.DEADLINE_MARGIN_S


def test_a_dependency_chain_lengthens_the_wait_past_either_timeout(tmp_path):
    """The red half of a budget sized from W alone: two chained tags need both timeouts."""
    chained = [_ks("bento-pdf", "10m"), _ks("sonarr", "10m", "bento-pdf")]
    alone = [_ks("bento-pdf", "10m"), _ks("sonarr", "10m")]
    waits = [
        _budget(
            soak.soak_files(R1, TAGS, DIRECTORIES, rules=tmp_path, kustomizations=k)
        )[0]
        for k in (chained, alone)
    ]
    assert waits[0] > waits[1] >= 10 * 60


@pytest.mark.parametrize("rollout, long", [("50m", True), ("1m", False)])
def test_bundle_files_sizes_the_wait_from_a_roles_rollout_timeout(
    tmp_path, rollout, long
):
    """`bundle_files` must hand its Kustomizations to the soak, or a 50m tag outlasts it.

    The 1m half is the red one: the same render under 3000s shows that the wait follows the
    role's timeout, rather than clearing 3000s whatever `soak_pair` was handed (#5389).
    """
    defaults = tmp_path / "sonarr" / "defaults"
    defaults.mkdir(parents=True)
    (defaults / "main.yml").write_text(f"manifests_rollout_timeout: {rollout}\n")
    files = _bundle(R1, tmp_path)
    sonarr = next(yaml_fast.safe_load_all(files["flux/sonarr.yaml"]))
    assert sonarr["spec"]["timeout"] == rollout
    assert (_budget(files)[0] > 3000) is long


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


def _grants(files: dict[str, str]) -> set[tuple[str, str, str, str]]:
    """Each `(binding kind, namespace or "", resource, verb)` the `soak` ServiceAccount holds."""
    docs = list(yaml_fast.safe_load_all(files["cluster/soak/rbac.yaml"]))
    roles = {
        (d["kind"], d["metadata"].get("namespace", "")): d["rules"]
        for d in docs
        if d["kind"] in ("Role", "ClusterRole")
    }
    found = set()
    for binding in (d for d in docs if d["kind"].endswith("Binding")):
        assert binding["subjects"] == [
            {"kind": "ServiceAccount", "name": "soak", "namespace": "flux-system"}
        ]
        namespace = binding["metadata"].get("namespace", "")
        for rule in roles[(binding["roleRef"]["kind"], namespace)]:
            found |= {
                (binding["kind"], namespace, resource, verb)
                for resource in rule["resources"]
                for verb in rule["verbs"]
            }
    return found


def test_the_rbac_reads_kustomizations_and_every_namespaces_pods_and_replicasets(
    tmp_path,
):
    files = soak.soak_files(R1, TAGS, DIRECTORIES, rules=tmp_path)
    assert {
        ("RoleBinding", "flux-system", "kustomizations", "get"),
        ("RoleBinding", "flux-system", "kustomizations", "list"),
        ("RoleBinding", "flux-system", "ocirepositories", "get"),
        ("ClusterRoleBinding", "", "pods", "list"),
        ("ClusterRoleBinding", "", "replicasets", "get"),
        ("ClusterRoleBinding", "", "replicasets", "list"),
    } <= _grants(files)


def _namespaced_outside_flux_system(files: dict[str, str]) -> list[str]:
    return [
        f"{d['kind']}/{d['metadata']['namespace']}"
        for d in yaml_fast.safe_load_all(files["cluster/soak/rbac.yaml"])
        if d["metadata"].get("namespace", "flux-system") != "flux-system"
    ]


def test_no_rbac_object_needs_a_namespace_a_tag_creates(tmp_path):
    """#5380: `soak` has no `dependsOn` edges, so Flux may apply it before the tag that
    creates `media` on the tag's first revision, and a Role in `media` would fail the apply.

    `sonarr-deferred` creates `media` with its own Namespace document, and the map still
    scopes sonarr's pods to it.
    """
    files = soak.soak_files(R1, TAGS, DIRECTORIES, rules=tmp_path)
    services = json.loads(
        _doc(files, "cluster/soak/configmap.yaml")["data"]["services.json"]
    )
    assert services["sonarr"]["namespaces"] == ["media"]
    assert _namespaced_outside_flux_system(files) == []


def test_a_role_in_a_tags_namespace_is_what_the_check_refuses():
    """The red half: D5.3b2's per-namespace render put a Role and a RoleBinding in `media`."""
    role = {"kind": "Role", "metadata": {"name": "soak", "namespace": "media"}}
    files = {"cluster/soak/rbac.yaml": yaml.safe_dump(role)}
    assert _namespaced_outside_flux_system(files) == ["Role/media"]


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


def test_the_revision_env_var_is_read_after_the_flag_and_before_head(monkeypatch):
    monkeypatch.setenv("HOMELAB_RENDER_REVISION", "0" * 40)
    assert soak.resolve_revision(R1) == R1
    assert soak.resolve_revision(None) == "0" * 40


@pytest.mark.parametrize("given", ["nothex", "a" * 39, "A" * 40])
def test_a_revision_that_is_not_40_lowercase_hex_is_refused(given):
    with pytest.raises(ValueError, match="40-hex"):
        soak.resolve_revision(given)
