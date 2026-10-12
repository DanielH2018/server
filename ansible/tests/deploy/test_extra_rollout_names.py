"""How `_extra_rollouts` reads a role's `manifests_extra_rollouts` names.

A name the parser misses reads as ungated, and a name it invents reads as gated, so both
directions are pinned against real roles and synthetic ones.
"""

from _autodeploy import _K8S_ROLES, _deployment_templates, _template_texts
from _autodeploy_rollout import (
    _deployment_docs,
    _extra_rollouts,
    _primary_rollout_name,
    _ungated_deployment_count,
    _ungated_deployments,
)


def test_extra_rollouts_are_counted_as_gated() -> None:
    """prowlarr renders two Deployments and gates both, by name — it is not an offender.

    This pins the guard's matching model (identity, not count) against a role that actually
    has an extra: a rendered Deployment is gated only if its own resolved name equals the
    primary rollout name or appears in `manifests_extra_rollouts`. prowlarr stays on the
    denylist regardless — the migrating-state PVC/Recreate shape covered in its
    `k8s_autodeploy_reason`, unrelated to whether it gates cleanly — so this test is about the
    guard's model, not about prowlarr's eligibility.
    """
    prowlarr = _K8S_ROLES / "prowlarr"
    assert len(_deployment_templates(prowlarr)) == 2
    assert _primary_rollout_name(prowlarr) == "prowlarr"
    assert _extra_rollouts(prowlarr) == {"flaresolverr"}
    assert _deployment_docs(
        _template_texts(prowlarr)["deployment-flaresolverr.yaml.j2"]
    ) == [("Deployment", "flaresolverr")]
    assert _ungated_deployment_count(prowlarr) == 0


def test_extra_rollout_naming_the_wrong_deployment_reads_as_ungated(
    widget_role,
) -> None:
    """A typo'd or drifted `manifests_extra_rollouts` name doesn't gate anything real.

    Matching by count alone (rendered - 1 - len(extras) == 0) would read this as fully gated even
    though the declared extra's name matches neither rendered Deployment. Matching by identity
    catches it: the second Deployment's real name isn't in {primary, declared extra}.
    """
    role = widget_role(
        "- ansible.builtin.include_role:\n"
        "    name: k8s/manifests\n"
        "  vars:\n"
        "    manifests_service: widget\n"
        "    manifests_extra_rollouts:\n"
        "      - name: widget-typo\n",
        templates={},
    )
    (role / "templates" / "deployment.yaml.j2").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: widget\n"
    )
    (role / "templates" / "deployment-cache.yaml.j2").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: widget-cache\n"
    )
    assert _extra_rollouts(role) == {"widget-typo"}
    assert _ungated_deployments(role) == ["deployment-cache.yaml.j2"]
    assert _ungated_deployment_count(role) == 1


def test_extra_rollout_entry_with_a_second_key_hides_no_later_entry(
    widget_role,
) -> None:
    """An entry carrying `restart_on: []` on its own line leaves the entries after it readable.

    The old pattern stopped at the first line that was not `- name:`, so `widget-cache` read as
    ungated and karakeep had to keep its `restart_on` entry last (#4578). A sibling var's own
    `- name:` list after the block still credits nothing, nor does the next task's `- name:`
    across a blank line, which the old `\\s*` crossed to credit cloudflare-ddns with `Wait`.
    """
    role = widget_role(
        "- ansible.builtin.include_role:\n"
        "    name: k8s/manifests\n"
        "  vars:\n"
        "    manifests_extra_rollouts:\n"
        "      - name: widget-chrome\n"
        "        restart_on: []\n"
        "      - name: widget-agent\n"
        "        kind: daemonset\n"
        "        restart_on:\n"
        "          - config\n"
        "      - name: widget-cache\n"
        "    manifests_self_rollouts:\n"
        "      - name: not-an-extra\n",
        templates={},
    )
    assert _extra_rollouts(role) == {"widget-chrome", "widget-agent", "widget-cache"}
    followed_by_a_task = widget_role(
        "- ansible.builtin.include_role:\n"
        "    name: k8s/manifests\n"
        "  vars:\n"
        "    manifests_extra_rollouts:\n"
        "      - name: widget-direct\n"
        "\n"
        "- name: Wait for the widget rollouts\n",
        templates={},
    )
    assert _extra_rollouts(followed_by_a_task) == {"widget-direct"}
    assert {"karakeep-meilisearch", "karakeep-time-tagger", "karakeep-chrome"} == (
        _extra_rollouts(_K8S_ROLES / "karakeep")
    )
