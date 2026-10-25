"""The drill's rendered RBAC grants every call its script makes (H2.3d).

`test_rbac_against_script.py` holds the derivation's own cases and the drill as its named member.
This file holds the case for the real role: the Roles and the ClusterRole that
`ansible/roles/k8s/longhorn-restore-drill/templates/rbac.yaml.j2` renders against the set the
real script derives, and the red half that drops `watch` on volumes.

Run: uv run pytest ansible/tests/k8s/test_rbac_against_script_drill.py
"""

import re

from _k8s_render import render_role_template
from _script_rbac import grants, missing, shell_needs
from _shell_render import render_shell_script
from lib import yaml_fast

DRILL_NAMESPACES = {"$PVC_NS": "homelab", "$STATE_NS": "homelab"}
DRILL_TEMPLATE = ("k8s", "longhorn-restore-drill", "longhorn-restore-drill.sh.j2")


def _drill_script() -> str:
    """The real script, with the stamp ConfigMap's name read off its own assignment.

    The script holds the name in `$STATE_CM`, and a name held in a variable cannot be checked
    against a `resourceNames` rule, so the one assignment is substituted into the calls.
    """
    script = render_shell_script(*DRILL_TEMPLATE)
    match = re.search(r'^STATE_CM="([^"]+)"$', script, re.M)
    assert match, "the drill no longer assigns STATE_CM literally — update this reader"
    return script.replace('"$STATE_CM"', match.group(1))


def test_the_drill_role_grants_every_call_its_script_makes() -> None:
    """H2.3d: the rendered Roles and ClusterRole hold the set the real script derives."""
    script = _drill_script()
    docs = [
        doc
        for doc in yaml_fast.safe_load_all(
            render_role_template("longhorn-restore-drill", "rbac.yaml.j2")
        )
        if doc
    ]
    needs = shell_needs(script, DRILL_NAMESPACES)
    assert len(needs) > 10, "the derivation read the script as nearly empty"
    granted = grants(docs, "homelab", "longhorn-restore-drill")
    assert missing(needs, granted) == []


def test_a_drill_role_without_the_volume_delete_wait_is_flagged() -> None:
    """Red half: drop `watch` on volumes, and kubectl skips the teardown wait without an error."""
    script = _drill_script()
    docs = [
        doc
        for doc in yaml_fast.safe_load_all(
            render_role_template("longhorn-restore-drill", "rbac.yaml.j2")
        )
        if doc
    ]
    for doc in docs:
        for rule in doc.get("rules", []):
            if rule["resources"] == ["volumes"]:
                rule["verbs"] = [v for v in rule["verbs"] if v != "watch"]
    gaps = missing(
        shell_needs(script, DRILL_NAMESPACES),
        grants(docs, "homelab", "longhorn-restore-drill"),
    )
    assert any(gap[2:4] == ("volumes", "watch") for gap in gaps), gaps
