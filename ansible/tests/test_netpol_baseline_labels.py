"""Guards the opt-in label the netpol-baseline policy selects on.

`roles/k8s/netpol-baseline` fences exactly the pods carrying `netpol-baseline: enforced` in
their POD TEMPLATE. Nothing else notices when that label goes missing: the probe job dials
only littlelink, so dropping the label from any of the other five leaves them accepting
connections from every pod in the cluster while the probe still prints four green lines.

Asserted against the RENDERED manifests, not a text scan, because the failure that matters is
the label sitting in `spec.selector.matchLabels` only — a file-level grep for the string passes
on exactly that broken shape, and a NetworkPolicy does not look at a Deployment's selector.

Run: uv run pytest ansible/tests/test_netpol_baseline_labels.py
"""

from __future__ import annotations

from _k8s_render import rendered_docs

# Slice 1 of the rollout: the six traefik-only leaf apps (docs/networkpolicy-default-deny.md).
# Adding a role here without labelling it — or labelling one without listing it — fails below.
SLICE_1_ROLES = {
    "bento-pdf",
    "littlelink",
    "speedtest",
    "healthchecks",
    "ical-proxy",
    "code-server",
}

LABEL = ("netpol-baseline", "enforced")


def _labelled_roles() -> set[str]:
    key, value = LABEL
    return {
        role
        for role, _tpl, doc in rendered_docs()
        if (doc.get("spec") or {})
        .get("template", {})
        .get("metadata", {})
        .get("labels", {})
        .get(key)
        == value
    }


def test_exactly_the_slice_1_roles_carry_the_baseline_label() -> None:
    labelled = _labelled_roles()
    missing = sorted(SLICE_1_ROLES - labelled)
    extra = sorted(labelled - SLICE_1_ROLES)
    assert not missing and not extra, (
        "netpol-baseline: enforced no longer matches slice 1.\n"
        f"  missing (silently unfenced, and the probe would not notice): {missing}\n"
        f"  unexpected (fenced without a probe proving its callers still work): {extra}\n"
        "Update SLICE_1_ROLES together with docs/networkpolicy-default-deny.md when the "
        "rollout moves to the next slice."
    )
