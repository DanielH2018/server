"""Every PriorityClass the `flux` render names exists before Flux runs (H1.2).

The API server's Priority admission plugin refuses a pod that names a class that does not exist.
Flux's controllers run at `homelab-critical`, and the `flux` role is the first to start on a
rebuilt cluster, so the class has to exist by then. The operator applies the `cluster-base` tag
by hand before the `flux` role (docs/longhorn-disaster-recovery.md, *Procedure (fresh host,
total loss)*, step 2). That works only while `cluster-base` renders every class `flux` names.

Run: uv run pytest ansible/tests/k8s/test_flux_priority_classes_come_from_cluster_base.py
"""

from _k8s_render import pod_spec, rendered_docs

# The classes Kubernetes creates itself, so no render has to carry them.
BUILT_IN_CLASSES = frozenset({"system-cluster-critical", "system-node-critical"})


def missing_classes(
    named: set[str], rendered: set[str], built_in: frozenset[str] = BUILT_IN_CLASSES
) -> list[str]:
    """The classes in `named` that are neither built in nor in `rendered`, sorted."""
    return sorted(named - rendered - built_in)


def _flux_classes() -> set[str]:
    return {
        pod_spec(doc)["priorityClassName"]
        for role, _tpl, doc in rendered_docs()
        if role == "flux" and pod_spec(doc).get("priorityClassName")
    }


def _cluster_base_classes() -> set[str]:
    return {
        doc["metadata"]["name"]
        for role, _tpl, doc in rendered_docs()
        if role == "cluster-base" and doc.get("kind") == "PriorityClass"
    }


def test_every_flux_priority_class_is_built_in_or_rendered_here():
    # fact: ansible/roles/k8s/cluster-base/CLAUDE.md#At a glance
    named = _flux_classes()
    assert "homelab-critical" in named, "the flux render no longer names its own class"
    assert "homelab-critical" in _cluster_base_classes()
    assert missing_classes(named, _cluster_base_classes()) == []


def test_a_flux_render_naming_a_class_cluster_base_lacks_is_flagged():
    rendered = {"homelab-critical", "homelab-standard"}
    assert missing_classes({"homelab-critical", "homelab-flux"}, rendered) == [
        "homelab-flux"
    ]
    assert missing_classes({"system-node-critical", "homelab-critical"}, rendered) == []
