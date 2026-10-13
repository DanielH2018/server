"""kube-state-metrics' Flux config must read the commit field, and its ClusterRole must grant it.

Both failures are quiet. A kind the config reads without a list/watch grant makes the collector
log RBAC errors every resync and export nothing for that kind, so gotk_resource_info simply has
no series. A Kustomization `revision` read from `status.lastAppliedRevision`, the field upstream's
example reads, carries `prod@sha256:<digest>`, which names no commit, and every reader contracts.md
C6 sends to the label would then find no `master@sha1:<sha>` to match.

The config is read back out of the rendered Deployment argument, not the defaults variable, so a
template that stops passing it fails here too.
"""

import copy
import json

from lib import yaml_fast

from _k8s_render import render_role_template

_ARG = "--custom-resource-state-config="


def _docs() -> list[dict]:
    text = render_role_template("observability", "kube-state-metrics.yaml.j2")
    return [d for d in yaml_fast.safe_load_all(text) if d]


def _config(docs: list[dict]) -> dict:
    deployment = next(d for d in docs if d["kind"] == "Deployment")
    args = deployment["spec"]["template"]["spec"]["containers"][0]["args"]
    matches = [a for a in args if a.startswith(_ARG)]
    assert len(matches) == 1, f"expected one {_ARG} argument, got {len(matches)}"
    return json.loads(matches[0].removeprefix(_ARG))


def _rules(docs: list[dict]) -> list[dict]:
    return next(d for d in docs if d["kind"] == "ClusterRole")["rules"]


def _resources(config: dict) -> list[dict]:
    return config["spec"]["resources"]


def _plural(kind: str) -> str:
    lower = kind.lower()
    return lower[:-1] + "ies" if lower.endswith("y") else lower + "s"


def _granted(rules: list[dict], group: str, resource: str) -> bool:
    return any(
        group in rule["apiGroups"]
        and resource in rule["resources"]
        and {"list", "watch"} <= set(rule["verbs"])
        for rule in rules
    )


def _ungranted(config: dict, rules: list[dict]) -> set[str]:
    """Each `<group>/<plural>` the config reads, or the CRDs it watches, with no list/watch."""
    wanted = {
        (r["groupVersionKind"]["group"], _plural(r["groupVersionKind"]["kind"]))
        for r in _resources(config)
    }
    wanted.add(("apiextensions.k8s.io", "customresourcedefinitions"))
    return {f"{g}/{res}" for g, res in wanted if not _granted(rules, g, res)}


def _kustomization_revision(config: dict) -> list[str]:
    resource = next(
        r
        for r in _resources(config)
        if r["groupVersionKind"]["kind"] == "Kustomization"
    )
    return resource["metrics"][0]["labelsFromPath"]["revision"]


def _reads_origin_revision(config: dict) -> bool:
    return _kustomization_revision(config) == ["status", "lastAppliedOriginRevision"]


def test_every_flux_kind_the_config_reads_is_granted() -> None:
    docs = _docs()
    config = _config(docs)
    kinds = {r["groupVersionKind"]["kind"] for r in _resources(config)}
    assert kinds == {"Kustomization", "OCIRepository"}
    assert not _ungranted(config, _rules(docs)), (
        f"{sorted(_ungranted(config, _rules(docs)))} have no list/watch in the "
        "kube-state-metrics ClusterRole, so the collector exports nothing for them"
    )


def test_a_flux_kind_without_a_grant_is_flagged() -> None:
    docs = _docs()
    config = copy.deepcopy(_config(docs))
    _resources(config).append(
        {
            "groupVersionKind": {
                "group": "helm.toolkit.fluxcd.io",
                "version": "v2",
                "kind": "HelmRelease",
            }
        }
    )
    assert _ungranted(config, _rules(docs)) == {"helm.toolkit.fluxcd.io/helmreleases"}


def test_the_kustomization_revision_reads_the_origin_field() -> None:
    assert _reads_origin_revision(_config(_docs())), (
        "gotk_resource_info's Kustomization revision must name a commit (contracts.md C6)"
    )


def test_a_revision_read_from_the_digest_field_is_flagged() -> None:
    config = copy.deepcopy(_config(_docs()))
    _kustomization_revision(config)[:] = ["status", "lastAppliedRevision"]
    assert not _reads_origin_revision(config)
