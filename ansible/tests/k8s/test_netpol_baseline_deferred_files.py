"""Holds every netpol-baseline policy outside k8s_namespace to `manifests_deferred_files`.

Run: uv run pytest ansible/tests/k8s/test_netpol_baseline_deferred_files.py
"""

from _helpers import K8S_ROLES
from _k8s_render import host_context, rendered_docs
from lib import yaml_fast

TASKS = (K8S_ROLES / "netpol-baseline" / "tasks" / "main.yml").read_text()

# The armed apply runs `-n {{ k8s_namespace }}` and refuses a document that names another
# namespace (#3575), so every such template has to be in `manifests_deferred_files`. Nothing else
# notices a missing entry: prek and the suite pass, and `--dry-run` never shows a deferred
# document to the API server. The omission surfaces only as a failed deploy.
FOREIGN_NAMESPACE_TEMPLATES = {
    "networkpolicy-observability.yaml",
    "networkpolicy-prometheus.yaml",
    "networkpolicy-loki.yaml",
    "networkpolicy-alertmanager.yaml",
}


def _deferred_files() -> list[str]:
    (deploy,) = [
        task
        for task in yaml_fast.safe_load(TASKS)
        if "manifests_deferred_files" in (task.get("vars") or {})
    ]
    return deploy["vars"]["manifests_deferred_files"]


def foreign_namespace_templates(
    docs: list[tuple[str, dict]], namespace: str
) -> set[str]:
    """The deployed basenames of the (template, doc) pairs whose doc names another namespace.

    A doc with no `metadata.namespace` takes the apply's `-n`, so it is not foreign.
    """
    return {
        tpl.removesuffix(".j2")
        for tpl, doc in docs
        if doc.get("metadata", {}).get("namespace", "") not in ("", namespace)
    }


def _netpol_baseline_policies() -> list[tuple[str, dict]]:
    return [
        (tpl, doc)
        for role, tpl, doc in rendered_docs()
        if role == "netpol-baseline" and tpl.startswith("networkpolicy")
    ]


def test_the_foreign_namespace_census_finds_the_observability_policies() -> None:
    """Must-find: a census that rendered nothing, or read the wrong namespace, finds none."""
    found = foreign_namespace_templates(
        _netpol_baseline_policies(), host_context()["k8s_namespace"]
    )
    assert found == FOREIGN_NAMESPACE_TEMPLATES


def undeferred(
    docs: list[tuple[str, dict]], namespace: str, deferred: list[str]
) -> list[str]:
    """The foreign-namespace templates the armed apply would be handed, and refuse."""
    return sorted(foreign_namespace_templates(docs, namespace) - set(deferred))


def test_every_foreign_namespace_policy_is_deferred() -> None:
    missing = undeferred(
        _netpol_baseline_policies(), host_context()["k8s_namespace"], _deferred_files()
    )
    assert not missing, (
        f"netpol-baseline templates render into a namespace other than k8s_namespace but are "
        f"missing from manifests_deferred_files in tasks/main.yml: {missing}\n"
        "The armed apply runs -n k8s_namespace and refuses them, so the deploy would fail. Add "
        "each to the list so the role's own apply of the deferred directory picks it up."
    )


def test_a_foreign_namespace_policy_missing_from_the_deferred_list_is_flagged() -> None:
    """The red fixture: the real deferred list with alertmanager's entry dropped."""
    deferred = [f for f in _deferred_files() if f != "networkpolicy-alertmanager.yaml"]
    missing = undeferred(
        _netpol_baseline_policies(), host_context()["k8s_namespace"], deferred
    )
    assert missing == ["networkpolicy-alertmanager.yaml"]
