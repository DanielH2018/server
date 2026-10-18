"""A moved job's rendered RBAC grants every call its script makes, and nothing proves it by luck.

Target slice H2.1 lands the derivation in `_script_rbac.py` before any cron moves. Each H2 family
PR that turns a host cron into a CronJob adds a case here that derives its script's needs and
checks them against that job's rendered Role or ClusterRole: the drill at H2.3c, the CrowdSec
jobs at H2.4b and H2.4c, and the *arr checks at H2.5b to H2.5d. Until then the real drill is the
named member: its derived set must hold `get` on its backup target and the `watch` its teardown
needs, or the derivation reads nothing.

Run: uv run pytest ansible/tests/k8s/test_rbac_against_script.py
"""

import importlib
import json

import pytest
from _helpers import K8S_ROLES
from _k8s_render import render_role_template
from _script_rbac import (
    ALL_NAMESPACES,
    grants,
    missing,
    recorded_needs,
    shell_needs,
)
from _shell_render import render_shell_script
from lib import yaml_fast

# The drill restores into the claim's own namespace, and every claim it can pick is in
# `homelab` (the plan's *The drill: state into a ConfigMap first, then the move*).
DRILL_NAMESPACES = {"$PVC_NS": "homelab"}


def _role(rules: list[dict], namespace: str | None = "homelab") -> list[dict]:
    """A ServiceAccount `job` bound to one Role in `namespace`, or to a ClusterRole for None."""
    cluster = namespace is None
    role = {
        "kind": "ClusterRole" if cluster else "Role",
        "metadata": {"name": "job", **({} if cluster else {"namespace": namespace})},
        "rules": rules,
    }
    binding = {
        "kind": "ClusterRoleBinding" if cluster else "RoleBinding",
        "metadata": {"name": "job", **({} if cluster else {"namespace": namespace})},
        "roleRef": {"kind": role["kind"], "name": "job"},
        "subjects": [{"kind": "ServiceAccount", "name": "job", "namespace": "homelab"}],
    }
    return [role, binding]


def _gaps(needs: set[tuple], docs: list[dict]) -> list[tuple]:
    return missing(needs, grants(docs, "homelab", "job"))


def test_the_real_drill_needs_its_backup_target_and_the_delete_wait() -> None:
    """The named member: the drill's script, as the host runs it, read through the derivation."""
    script = render_shell_script("setup", "k3s", "longhorn-restore-drill.sh.j2")
    # The calls' shape, without the object names, which the drill holds in variables.
    needs = {need[:4] for need in shell_needs(script, DRILL_NAMESPACES)}
    assert ("longhorn-system", "longhorn.io", "backuptargets", "get") in needs
    assert ("longhorn-system", "longhorn.io", "volumes", "watch") in needs
    # An unnamed `get ... 2>/dev/null` is a list: the redirect's `2` is not a resource name.
    assert ("longhorn-system", "longhorn.io", "backups", "list") in needs
    # The heredoc's cluster-scoped PersistentVolume, applied: read, then created or patched.
    assert {
        (None, "", "persistentvolumes", v) for v in ("get", "create", "patch")
    } <= needs
    assert ("homelab", "", "pods/log", "get") in needs


def test_a_script_line_with_no_grant_is_flagged() -> None:
    needs = shell_needs(
        "URL=$($KUBECTL -n longhorn-system get backuptarget x -o name)\n"
    )
    docs = _role(
        [{"apiGroups": ["longhorn.io"], "resources": ["volumes"], "verbs": ["get"]}],
        namespace="longhorn-system",
    )
    assert _gaps(needs, docs) == [
        ("longhorn-system", "longhorn.io", "backuptargets", "get", "x")
    ]


def test_a_script_line_with_its_grant_is_clean() -> None:
    needs = shell_needs(
        "URL=$($KUBECTL -n longhorn-system get backuptarget x -o name)\n"
    )
    docs = _role(
        [
            {
                "apiGroups": ["longhorn.io"],
                "resources": ["backuptargets"],
                "verbs": ["get"],
            }
        ],
        namespace="longhorn-system",
    )
    assert _gaps(needs, docs) == []


_PV_APPLY = (
    'cat >"$MANIFEST" <<YAML\napiVersion: v1\nkind: PersistentVolume\nmetadata:\n'
    "  name: x\nYAML\n"
    '$KUBECTL apply -f "$MANIFEST" >/dev/null 2>&1 || fail "no"\n'
)


def test_an_applied_kind_without_patch_is_flagged() -> None:
    docs = _role(
        [
            {
                "apiGroups": [""],
                "resources": ["persistentvolumes"],
                "verbs": ["get", "create"],
            }
        ],
        namespace=None,
    )
    assert _gaps(shell_needs(_PV_APPLY), docs) == [
        (None, "", "persistentvolumes", "patch", "x")
    ]


def test_an_applied_kind_with_get_create_and_patch_is_clean() -> None:
    verbs = ["get", "create", "patch"]
    docs = _role(
        [{"apiGroups": [""], "resources": ["persistentvolumes"], "verbs": verbs}],
        namespace=None,
    )
    assert _gaps(shell_needs(_PV_APPLY), docs) == []


_INGRESSROUTES = {
    "apiGroups": ["traefik.io"],
    "resources": ["ingressroutes"],
    "verbs": ["list"],
}


def test_an_all_namespaces_read_under_a_namespaced_role_is_flagged() -> None:
    needs = recorded_needs([["kubectl", "get", "ingressroute", "-A", "-o", "json"]])
    assert _gaps(needs, _role([_INGRESSROUTES])) == [
        (ALL_NAMESPACES, "traefik.io", "ingressroutes", "list", None)
    ]


def test_an_all_namespaces_read_under_a_clusterrole_is_clean() -> None:
    needs = recorded_needs([["kubectl", "get", "ingressroute", "-A", "-o", "json"]])
    assert _gaps(needs, _role([_INGRESSROUTES], namespace=None)) == []


_LOGS = [
    ["k3s", "kubectl", "-n", "homelab", "logs", "deploy/janitorr", "--tail", "200"]
]
_POD_LOG_READS = [
    {"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]},
    {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
]


def test_logs_of_a_deployment_with_no_deployment_grant_is_flagged() -> None:
    assert _gaps(recorded_needs(_LOGS), _role(_POD_LOG_READS)) == [
        ("homelab", "apps", "deployments", "get", "janitorr")
    ]


def test_logs_of_a_deployment_with_its_three_grants_is_clean() -> None:
    deployments = {
        "apiGroups": ["apps"],
        "resources": ["deployments"],
        "verbs": ["get"],
    }
    assert _gaps(recorded_needs(_LOGS), _role([*_POD_LOG_READS, deployments])) == []


_DELETE_PVC = (
    '$KUBECTL -n "$PVC_NS" delete pvc "$DRILL" --ignore-not-found >/dev/null 2>&1\n'
)


def test_a_delete_granted_without_list_and_watch_is_flagged() -> None:
    """kubectl skips the wait and exits 0 when either is Forbidden, so teardown order is lost."""
    needs = shell_needs(_DELETE_PVC, DRILL_NAMESPACES)
    docs = _role(
        [
            {
                "apiGroups": [""],
                "resources": ["persistentvolumeclaims"],
                "verbs": ["delete"],
            }
        ]
    )
    assert _gaps(needs, docs) == [
        ("homelab", "", "persistentvolumeclaims", "list", "$DRILL"),
        ("homelab", "", "persistentvolumeclaims", "watch", "$DRILL"),
    ]


def test_a_delete_granted_with_list_and_watch_is_clean() -> None:
    needs = shell_needs(_DELETE_PVC, DRILL_NAMESPACES)
    verbs = ["delete", "list", "watch"]
    docs = _role(
        [{"apiGroups": [""], "resources": ["persistentvolumeclaims"], "verbs": verbs}]
    )
    assert _gaps(needs, docs) == []


def test_an_unresolved_namespace_variable_is_flagged() -> None:
    with pytest.raises(KeyError, match="PVC_NS"):
        shell_needs(_DELETE_PVC)


def test_an_unknown_verb_is_flagged() -> None:
    with pytest.raises(KeyError, match="cordon"):
        recorded_needs([["kubectl", "cordon", "node-a"]])


# The drill's stamp ConfigMap grant from the plan (H2.3c): a get and a patch on one name.
_STAMPS = {
    "apiGroups": [""],
    "resources": ["configmaps"],
    "verbs": ["get", "patch"],
    "resourceNames": ["longhorn-restore-drill-stamps"],
}


def test_a_name_restricted_rule_is_flagged_for_another_name() -> None:
    needs = shell_needs("$KUBECTL -n homelab patch configmap other-cm -p '{}'\n")
    assert _gaps(needs, _role([_STAMPS])) == [
        ("homelab", "", "configmaps", "patch", "other-cm")
    ]


def test_a_name_restricted_rule_is_clean_for_its_own_name() -> None:
    needs = shell_needs(
        "$KUBECTL -n homelab patch configmap longhorn-restore-drill-stamps -p '{}'\n"
    )
    assert _gaps(needs, _role([_STAMPS])) == []


def test_a_name_restricted_rule_never_covers_create_or_an_unnamed_list() -> None:
    """RBAC cannot narrow `create` by name, and a list without a name selects every object."""
    rule = {**_STAMPS, "verbs": ["get", "create", "patch", "list"]}
    needs = shell_needs(
        'cat >"$M" <<YAML\napiVersion: v1\nkind: ConfigMap\nmetadata:\n'
        "  name: longhorn-restore-drill-stamps\n  namespace: homelab\nYAML\n"
        '$KUBECTL apply -f "$M"\n'
        "$KUBECTL -n homelab get configmap -o name\n"
    )
    assert _gaps(needs, _role([rule])) == [
        ("homelab", "", "configmaps", "create", "longhorn-restore-drill-stamps"),
        ("homelab", "", "configmaps", "list", None),
    ]


@pytest.mark.parametrize(
    "objects",
    [
        "configmap longhorn-restore-drill-stamps other-cm",
        "cm/longhorn-restore-drill-stamps cm/other-cm",
    ],
)
def test_a_second_named_object_the_rule_omits_is_flagged(objects: str) -> None:
    """Each name in one call is authorized on its own, so the one the rule omits is a gap."""
    needs = shell_needs(f"$KUBECTL -n homelab get {objects}\n")
    assert _gaps(needs, _role([_STAMPS])) == [
        ("homelab", "", "configmaps", "get", "other-cm")
    ]


def test_label_values_are_not_read_as_object_names() -> None:
    needs = shell_needs(
        "$KUBECTL -n homelab label configmap longhorn-restore-drill-stamps a=b c-\n"
    )
    assert _gaps(needs, _role([_STAMPS])) == []


def test_a_variable_name_against_a_name_restricted_rule_is_flagged() -> None:
    needs = shell_needs('$KUBECTL -n homelab patch configmap "$CM" -p "{}"\n')
    with pytest.raises(KeyError, match="variable"):
        _gaps(needs, _role([_STAMPS]))


def test_every_call_on_a_line_is_read() -> None:
    """The second call after a pipe or `&&` has needs of its own, and its gap is flagged."""
    needs = shell_needs(
        "A=$($KUBECTL -n homelab get pod x) && $KUBECTL -n homelab delete pvc y\n"
    )
    pods = {"apiGroups": [""], "resources": ["pods"], "verbs": ["get"]}
    assert _gaps(needs, _role([pods])) == [
        ("homelab", "", "persistentvolumeclaims", v, "y")
        for v in ("delete", "list", "watch")
    ]


def test_the_configarr_health_role_grants_every_call_its_reader_makes(
    monkeypatch,
) -> None:
    """H2.5b: configarr_health.py's main, run with a recording runner, against its rendered Role.

    The replies walk the reader down its longest path, a finished Job and then its log, so
    every call it can make is recorded.
    """
    monkeypatch.syspath_prepend(str(K8S_ROLES / "configarr" / "files"))
    reader = importlib.import_module("configarr_health")
    job = {
        "metadata": {"name": "configarr-1", "labels": {"app": "configarr"}},
        "status": {"succeeded": 1, "completionTime": "2026-10-18T04:30:26Z"},
    }
    replies = {"get": json.dumps({"items": [job]}), "logs": "Done\n"}
    argvs = []

    def record(*args: str) -> tuple[int, str]:
        argvs.append(["kubectl", "-n", "homelab", *args])
        return 0, replies[args[0]]

    monkeypatch.delenv("KUMA_PUSH_URL", raising=False)
    assert reader.main(kubectl=record) == 0
    assert [argv[3] for argv in argvs] == ["get", "logs"]
    docs = list(
        yaml_fast.safe_load_all(
            render_role_template("configarr", "health-rbac.yaml.j2")
        )
    )
    needs = recorded_needs(argvs)
    # The plan's grant set for this reader (*The *arr checks*), so a recorder that stopped
    # recording cannot pass on an empty set.
    assert {need[:4] for need in needs} == {
        ("homelab", "batch", "jobs", "list"),
        ("homelab", "batch", "jobs", "get"),
        ("homelab", "", "pods", "list"),
        ("homelab", "", "pods/log", "get"),
    }
    assert missing(needs, grants(docs, "homelab", "configarr-health")) == []
