"""Read-only RBAC: the ServiceAccount Claude uses, and the two dashboards that bind like it.

The read-only ClusterRole is the reason Ansible is the only write path to this cluster, so a
widened verb or a binding to a writing role is a silent privilege grant. Headlamp and the
homepage Kubernetes widget carry their own cluster identities and are held to the same rule.
"""

import yaml
from _helpers import ANSIBLE
from _manifest_guards import (
    ALL_VARS,
    K3S,
    K3S_DEFAULTS,
    K8S,
    _k8s_entries,
    _render,
    _role_defaults,
)


READ_VERBS = {"get", "list", "watch"}


def _readonly_rbac_docs() -> list[dict]:
    rendered = _render(
        K3S / "templates" / "readonly-rbac.yaml.j2",
        sys_user=ALL_VARS["sys_user"],
        k3s_readonly_sa_name=K3S_DEFAULTS["k3s_readonly_sa_name"],
        k3s_readonly_sa_namespace=K3S_DEFAULTS["k3s_readonly_sa_namespace"],
        k3s_readonly_crd_api_groups=K3S_DEFAULTS["k3s_readonly_crd_api_groups"],
    )
    return [d for d in yaml.safe_load_all(rendered) if d]


def _readonly_rules() -> list[dict]:
    return [
        rule
        for doc in _readonly_rbac_docs()
        if doc["kind"] == "ClusterRole"
        for rule in doc["rules"]
    ]


# Resources that turn cluster read access into cluster compromise. `secrets` is every
# credential the cluster holds — the built-in `view` role excludes it deliberately and the
# additive role must not put it back. `pods/exec` and its siblings are arbitrary code
# execution inside a running workload, which RBAC models as a subresource `create` but which
# reads, in a list of get/list/watch, like just more access.
FORBIDDEN_RESOURCES = {"secrets", "pods/exec", "pods/attach", "pods/portforward"}


def _grant_violations(rules: list[dict]) -> list[str]:
    """Every way a rule list exceeds read-only. Empty means the ceiling holds."""
    problems = []
    for rule in rules:
        groups = set(rule.get("apiGroups", []))
        named = set(rule.get("resources", []))
        extra = set(rule.get("verbs", [])) - READ_VERBS
        if extra:
            problems.append(f"verbs {sorted(extra)} on {sorted(named)}")
        if named & FORBIDDEN_RESOURCES:
            problems.append(f"resource {sorted(named & FORBIDDEN_RESOURCES)}")
        # A bare wildcard over the core group sweeps secrets back in without naming them.
        if "*" in named and ("" in groups or "*" in groups):
            problems.append(f"wildcard resources over apiGroups {sorted(groups)}")
    return problems


def test_readonly_role_stays_read_only():
    assert _grant_violations(_readonly_rules()) == []


def test_the_read_only_check_rejects_a_widened_role():
    """The guard above only means something if it fails on a role that oversteps.

    These are the three shapes a widening actually takes — a write verb, a named secret read, and a
    wildcard that never says "secrets" out loud.
    """
    for rule in (
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "delete"]},
        {"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"]},
        {"apiGroups": [""], "resources": ["*"], "verbs": ["get"]},
        {"apiGroups": [""], "resources": ["pods/exec"], "verbs": ["get"]},
    ):
        assert _grant_violations([rule]), f"widening not caught: {rule}"


def test_readonly_bindings_never_reference_a_writing_clusterrole():
    """The additive ClusterRole is audited by the tests above; a roleRef pointing somewhere
    else routes around all of them. Only `view` and this role's own name are permitted."""
    allowed = {"view", K3S_DEFAULTS["k3s_readonly_sa_name"]}
    bindings = [d for d in _readonly_rbac_docs() if d["kind"] == "ClusterRoleBinding"]
    assert bindings, "no ClusterRoleBinding rendered"
    for binding in bindings:
        name = binding["roleRef"]["name"]
        assert name in allowed, f"{binding['metadata']['name']} binds to '{name}'"


def _headlamp_rbac_docs() -> list[dict]:
    rendered = _render(
        K8S / "headlamp" / "templates" / "rbac.yaml.j2",
        **_role_defaults("headlamp"),
    )
    return [d for d in yaml.safe_load_all(rendered) if d]


def test_headlamp_cluster_identity_stays_read_only():
    """Headlamp runs with `-unsafe-use-service-account-token`, so it never asks the browser
    for a credential — every request that gets past Authelia acts as this ServiceAccount, and
    the ClusterIP Service is reachable from any pod besides. The SA's ceiling is therefore the
    dashboard's security boundary, not a defence-in-depth layer, and it gets the same guard as
    the shell's homelab-readonly identity above."""
    rules = [
        rule
        for doc in _headlamp_rbac_docs()
        if doc["kind"] == "ClusterRole"
        for rule in doc["rules"]
    ]
    assert rules, "no ClusterRole rendered"
    assert _grant_violations(rules) == []


def test_headlamp_binds_only_to_read_only_cluster_roles():
    """A roleRef pointing anywhere else routes around the rule audit above.

    Upstream's Helm chart binds `cluster-admin` — copying a fragment of it back in is the realistic
    mistake.
    """
    allowed = {"view", "headlamp-cluster-read"}
    bindings = [d for d in _headlamp_rbac_docs() if d["kind"] == "ClusterRoleBinding"]
    assert bindings, "no ClusterRoleBinding rendered"
    for binding in bindings:
        name = binding["roleRef"]["name"]
        assert name in allowed, f"{binding['metadata']['name']} binds to '{name}'"


def test_headlamp_keeps_its_serviceaccount_token_mounted():
    """The flag that removes the token prompt reads the projected SA token.

    Setting automountServiceAccountToken false — or omitting serviceAccountName, which silently
    falls back to the namespace `default` SA with no permissions — leaves a dashboard that loads,
    authenticates nobody, and shows an empty cluster.
    """
    doc = yaml.safe_load(
        _render(
            K8S / "headlamp" / "templates" / "deployment.yaml.j2",
            container_item=next(c for c in _k8s_entries() if c["name"] == "headlamp"),
            **_role_defaults("headlamp"),
        )
    )
    spec = doc["spec"]["template"]["spec"]
    assert spec["serviceAccountName"] == "headlamp"
    assert spec["automountServiceAccountToken"] is True
    args = spec["containers"][0]["args"]
    assert "-unsafe-use-service-account-token" in args


def test_homepage_kubernetes_widget_wiring_holds_together():
    """Three pieces have to agree or the widget renders EMPTY rather than erroring:

    the config must ask for cluster mode, the pod must name the SA that mode authenticates with, and
    that SA must be able to read the metrics API. Any one of them missing looks identical from the
    dashboard — a tile with no numbers, which reads as "nothing to report".
    """
    role = K8S / "homepage"
    assert "mode: cluster" in (role / "templates" / "kubernetes.yaml.j2").read_text()

    deployment = yaml.safe_load(
        _render(
            role / "templates" / "deployment.yaml.j2",
            container_item=next(c for c in _k8s_entries() if c["name"] == "homepage"),
            **_role_defaults("homepage"),
        )
    )
    assert deployment["spec"]["template"]["spec"]["serviceAccountName"] == "homepage"

    rbac = [
        d
        for d in yaml.safe_load_all(
            _render(role / "templates" / "rbac.yaml.j2", **_role_defaults("homepage"))
        )
        if d
    ]
    rules = [rule for d in rbac if d["kind"] == "ClusterRole" for rule in d["rules"]]
    assert _grant_violations(rules) == []
    assert any("metrics.k8s.io" in rule.get("apiGroups", []) for rule in rules), (
        "no metrics.k8s.io read: every CPU/memory figure in the widget would be blank"
    )


def test_readonly_role_covers_the_crd_groups_this_homelab_deploys():
    """`view` covers no CRDs and nothing aggregates into it, so a group missing from the
    list degrades silently: the kubeconfig still works, that one `kubectl get` says
    Forbidden, and the caller falls back to sudo — which is the thing this replaced."""
    groups = set(K3S_DEFAULTS["k3s_readonly_crd_api_groups"])
    route = (ANSIBLE / "templates" / "ingressroute.yml.j2").read_text()
    assert "traefik.io" in route, (
        "ingressroute macro no longer uses the traefik.io group"
    )
    assert "traefik.io" in groups, "IngressRoute/Middleware unreadable without sudo"
