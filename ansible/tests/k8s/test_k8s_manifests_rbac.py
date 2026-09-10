"""Read-only RBAC: the ServiceAccount Claude uses, and the two dashboards that bind like it.

The read-only ClusterRole is the reason Ansible is the only write path to this cluster, so a
widened verb or a binding to a writing role is a silent privilege grant. Headlamp and the
homepage Kubernetes widget carry their own cluster identities and are held to the same rule.
"""

import copy
import re

from lib import yaml_fast
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
    return [d for d in yaml_fast.safe_load_all(rendered) if d]


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
        # A proxy subresource is an HTTP request to whatever a Service or pod serves, made as
        # the API server. Unpinned, `get` on services/proxy reaches every Service in the
        # namespace, so a proxy grant must name its target (Headlamp: `prometheus:9090`).
        proxied = {r for r in named if r.endswith("/proxy")}
        if proxied and not rule.get("resourceNames"):
            problems.append(f"unpinned proxy grant on {sorted(proxied)}")
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
        {"apiGroups": [""], "resources": ["services/proxy"], "verbs": ["get"]},
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
    return [d for d in yaml_fast.safe_load_all(rendered) if d]


def test_headlamp_cluster_identity_stays_read_only():
    """Headlamp runs with `-unsafe-use-service-account-token`, so it never asks the browser
    for a credential — every request that gets past Authelia acts as this ServiceAccount, and
    the ClusterIP Service is reachable from any pod besides. The SA's ceiling is therefore the
    dashboard's security boundary, not a defence-in-depth layer, and it gets the same guard as
    the shell's homelab-readonly identity above."""
    rules = [
        rule
        for doc in _headlamp_rbac_docs()
        if doc["kind"] in {"ClusterRole", "Role"}
        for rule in doc["rules"]
    ]
    assert rules, "no ClusterRole rendered"
    assert _grant_violations(rules) == []


def test_headlamp_binds_only_to_read_only_cluster_roles():
    """A roleRef pointing anywhere else routes around the rule audit above.

    Upstream's Helm chart binds `cluster-admin` — copying a fragment of it back in is the realistic
    mistake. The one namespaced binding is the Prometheus proxy grant, audited above like the
    ClusterRoles; a second RoleBinding needs its own reason here.
    """
    allowed = {"view", "headlamp-cluster-read"}
    bindings = [d for d in _headlamp_rbac_docs() if d["kind"] == "ClusterRoleBinding"]
    assert bindings, "no ClusterRoleBinding rendered"
    for binding in bindings:
        name = binding["roleRef"]["name"]
        assert name in allowed, f"{binding['metadata']['name']} binds to '{name}'"
    role_bindings = [d for d in _headlamp_rbac_docs() if d["kind"] == "RoleBinding"]
    assert [b["roleRef"]["name"] for b in role_bindings] == [
        "headlamp-prometheus-proxy"
    ]
    assert role_bindings[0]["roleRef"]["kind"] == "Role"


def test_headlamp_prometheus_proxy_grant_names_the_labelled_service():
    """Two files must agree for a chart to render, and neither fails loudly when they don't.

    The plugin finds Prometheus by the `headlamp-prometheus` label on a Service, then proxies
    to `<name>:<port>` through the API server; the Role pins that exact string. A renamed
    Service, a moved port, or a dropped label leaves the plugin loaded and every chart empty.
    """
    from _k8s_render import rendered_docs

    services = [
        doc
        for _role, _tpl, doc in rendered_docs()
        if doc.get("kind") == "Service"
        and doc["metadata"].get("labels", {}).get("headlamp-prometheus") == "true"
    ]
    assert len(services) == 1, (
        f"expected exactly one labelled Prometheus Service, got {len(services)}"
    )
    svc = services[0]
    ports = [p["port"] for p in svc["spec"]["ports"]]
    assert len(ports) == 1, "the plugin tries every TCP port; keep the Service to one"
    expected = f"{svc['metadata']['name']}:{ports[0]}"

    roles = [d for d in _headlamp_rbac_docs() if d["kind"] == "Role"]
    assert len(roles) == 1
    assert roles[0]["metadata"]["namespace"] == svc["metadata"]["namespace"]
    (rule,) = roles[0]["rules"]
    assert rule["resources"] == ["services/proxy"]
    assert rule["verbs"] == ["get"]
    assert rule["resourceNames"] == [expected]


def test_headlamp_keeps_its_serviceaccount_token_mounted():
    """The flag that removes the token prompt reads the projected SA token.

    Setting automountServiceAccountToken false — or omitting serviceAccountName, which silently
    falls back to the namespace `default` SA with no permissions — leaves a dashboard that loads,
    authenticates nobody, and shows an empty cluster.
    """
    doc = yaml_fast.safe_load(
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


# The three bindings the OIDC identity has to join. Named rather than counted: the whole
# failure mode this guards is a binding that keeps the ServiceAccount and quietly loses the
# Group, and a count moves for a dozen innocent reasons while a name only goes missing when
# that happens.
HEADLAMP_BINDINGS = frozenset(
    {"headlamp-view", "headlamp-cluster-read", "headlamp-prometheus-proxy"}
)


def _bindings_missing_the_oidc_group(docs: list[dict], group: str) -> list[str]:
    """Bindings that grant the ServiceAccount something the OIDC Group does not get.

    Every name returned is a resource an OIDC login cannot see. Empty means the two identities
    have the same view.
    """
    missing = []
    for doc in docs:
        if doc.get("kind") not in {"ClusterRoleBinding", "RoleBinding"}:
            continue
        subjects = doc.get("subjects", [])
        has_sa = any(
            s.get("kind") == "ServiceAccount" and s.get("name") == "headlamp"
            for s in subjects
        )
        has_group = any(
            s.get("kind") == "Group" and s.get("name") == group for s in subjects
        )
        if has_sa and not has_group:
            missing.append(doc["metadata"]["name"])
    return missing


def test_headlamp_oidc_group_is_bound_wherever_the_serviceaccount_is():
    """The OIDC identity must see exactly what the ServiceAccount sees.

    Headlamp under OIDC forwards the browser's `id_token` and the API server authorises it, so
    the login arrives as a `User` in a `Group` and carries none of the SA's RBAC. A binding the
    Group is missing from is a resource list that comes back Forbidden — which the UI renders as
    an empty cluster, with a successful login in front of it and nothing in any log.
    """
    docs = _headlamp_rbac_docs()
    group = _role_defaults("headlamp")["headlamp_k8s_oidc_group"]
    bound = {
        doc["metadata"]["name"]
        for doc in docs
        if doc.get("kind") in {"ClusterRoleBinding", "RoleBinding"}
    }
    assert HEADLAMP_BINDINGS <= bound, (
        f"bindings went missing: {HEADLAMP_BINDINGS - bound}"
    )
    assert _bindings_missing_the_oidc_group(docs, group) == []


def test_the_oidc_group_check_rejects_a_binding_that_drops_the_group():
    """The rejecting half. Without it the check above passes on a template with no Group at
    all — every binding trivially satisfies "has the SA and the Group" once nothing has either.
    """
    docs = copy.deepcopy(_headlamp_rbac_docs())
    group = _role_defaults("headlamp")["headlamp_k8s_oidc_group"]
    victim = next(d for d in docs if d["metadata"]["name"] == "headlamp-view")
    victim["subjects"] = [s for s in victim["subjects"] if s.get("kind") != "Group"]
    assert _bindings_missing_the_oidc_group(docs, group) == ["headlamp-view"]


def test_headlamp_oidc_group_subjects_name_the_rbac_api_group():
    """A `kind: Group` subject without `apiGroup: rbac.authorization.k8s.io` is rejected by the
    API server on apply, which fails the deploy rather than degrading — but it fails it in the
    middle of a manifest sweep, so catch it in the render instead."""
    subjects = [
        s
        for doc in _headlamp_rbac_docs()
        if doc.get("kind") in {"ClusterRoleBinding", "RoleBinding"}
        for s in doc.get("subjects", [])
        if s.get("kind") == "Group"
    ]
    assert len(subjects) == len(HEADLAMP_BINDINGS)
    for subject in subjects:
        assert subject["apiGroup"] == "rbac.authorization.k8s.io"


def test_headlamp_oidc_group_carries_the_apiserver_prefix():
    """The group name is the Authelia group with the API server's `oidc-groups-prefix` on the
    front, and the prefix is the whole reason an Authelia group can never be read as a built-in
    `system:` group. A bare `admins` here means either the prefix was dropped from the API
    server (a collision class reopened) or the two spellings drifted (an empty dashboard).
    """
    group = _role_defaults("headlamp")["headlamp_k8s_oidc_group"]
    assert ":" in group, f"{group!r} carries no oidc-groups-prefix"
    assert not group.startswith("system:"), f"{group!r} impersonates a built-in group"


def _headlamp_pod_spec(**overrides) -> dict:
    """The rendered pod spec.

    `domain` is a SOPS value, so `_role_defaults` expands the URL defaults that read it with an
    empty host. The assertions below are therefore about each URL's SHAPE — which name it pins
    and which path it ends on — which is the property that has to hold anyway.
    """
    context = {
        "container_item": next(c for c in _k8s_entries() if c["name"] == "headlamp"),
        **_role_defaults("headlamp"),
        **overrides,
    }
    doc = yaml_fast.safe_load(
        _render(K8S / "headlamp" / "templates" / "deployment.yaml.j2", **context)
    )
    return doc["spec"]["template"]["spec"]


# The flags an OIDC-enabled Headlamp must carry. Named, because the failure mode of a missing
# one is never an error: no `-oidc-callback-url` and Headlamp builds the callback from the
# request host, no `-oidc-scopes=...groups` and the RBAC group subject matches nothing.
OIDC_ARG_NAMES = frozenset(
    {
        "-oidc-client-id",
        "-oidc-idp-issuer-url",
        "-oidc-scopes",
        "-oidc-callback-url",
        "-oidc-use-pkce",
    }
)


def _oidc_arg_names(args: list[str]) -> set[str]:
    return {arg.split("=", 1)[0] for arg in args if arg.startswith("-oidc-")}


def test_headlamp_with_oidc_off_browses_as_its_serviceaccount():
    """The default, and the state the cluster is in: no OIDC flags at all, the SA-token flag
    present. This is the half that must not change while the API server carries no
    `--kube-apiserver-arg=oidc-*` flags — dropping the SA-token flag before then leaves a
    dashboard that authenticates nobody.
    """
    args = _headlamp_pod_spec()["containers"][0]["args"]
    assert "-unsafe-use-service-account-token" in args
    assert _oidc_arg_names(args) == set()


def test_headlamp_with_oidc_on_swaps_the_serviceaccount_flag_for_the_oidc_flags():
    """The rejecting half of the pair above, and the arming state.

    The two identities are alternatives, not layers. Headlamp accepts both flag sets at once —
    `newInClusterContextFromConfig` in backend/pkg/kubeconfig/kubeconfig.go puts a TokenFile and
    an OidcConf on the same context — and then the SA token stays in force for API calls behind
    a sign-in button, which is worse than either state alone. So the SA flag has to GO here,
    and this test is what fails if a later edit makes the branches additive.
    """
    args = _headlamp_pod_spec(headlamp_k8s_oidc_enabled=True)["containers"][0]["args"]
    assert "-unsafe-use-service-account-token" not in args
    assert _oidc_arg_names(args) == OIDC_ARG_NAMES


def test_headlamp_never_passes_its_client_secret_as_an_argument():
    """An argument is part of the pod spec, and this cluster's read-only ServiceAccount can
    read Deployments — so a secret passed as `-oidc-client-secret=...` is readable by anything
    that can `kubectl get deploy`. It arrives as an env var from a Secret instead.
    """
    spec = _headlamp_pod_spec(headlamp_k8s_oidc_enabled=True)
    container = spec["containers"][0]
    assert not [a for a in container["args"] if a.startswith("-oidc-client-secret")]
    (env,) = [
        e for e in container["env"] if e["name"] == "HEADLAMP_CONFIG_OIDC_CLIENT_SECRET"
    ]
    assert env["valueFrom"]["secretKeyRef"] == {
        "name": "headlamp-oidc",
        "key": "client_secret",
    }
    assert "value" not in env


def test_headlamp_oidc_scopes_leave_openid_to_headlamp():
    """Headlamp prepends it: `Scopes: append([]string{oidc.ScopeOpenID}, ...Scopes...)` in
    backend/cmd/headlamp.go at v0.45.0. Listing it here sends it twice. `groups` is the scope
    that carries the RBAC subject, so its absence is a login onto an empty dashboard.
    """
    args = _headlamp_pod_spec(headlamp_k8s_oidc_enabled=True)["containers"][0]["args"]
    (scopes,) = [
        a.removeprefix("-oidc-scopes=") for a in args if a.startswith("-oidc-scopes=")
    ]
    listed = scopes.split(",")
    assert "openid" not in listed, "Headlamp prepends openid; listing it sends it twice"
    assert "groups" in listed


def test_headlamp_oidc_urls_pin_the_lan_names():
    """Authelia's `iss` follows the host the request arrived on, and the API server's
    `oidc-issuer-url` compares one value exactly — so both URLs are LAN-only, and the callback
    is pinned rather than derived from the request host as Headlamp would otherwise do.
    """
    args = _headlamp_pod_spec(headlamp_k8s_oidc_enabled=True)["containers"][0]["args"]
    flags = dict(a.split("=", 1) for a in args if a.startswith("-oidc-") and "=" in a)
    issuer = flags["-oidc-idp-issuer-url"]
    assert issuer.startswith("https://auth.local."), f"{issuer} is not the LAN portal"
    callback = flags["-oidc-callback-url"]
    assert callback.startswith("https://headlamp.local."), callback
    assert callback.endswith("/oidc-callback"), (
        f"{callback} is not the path Headlamp serves the callback on"
    )


def test_headlamp_keeps_the_serviceaccount_token_mounted_under_oidc_too():
    """`-in-cluster` calls rest.InClusterConfig(), which reads the API server address and the
    cluster CA from the projected mount and ERRORS without it. Turning the mount off along with
    the SA-token flag reads like tidying up and stops the pod from starting.
    """
    spec = _headlamp_pod_spec(headlamp_k8s_oidc_enabled=True)
    assert spec["automountServiceAccountToken"] is True
    assert spec["serviceAccountName"] == "headlamp"


def test_homepage_kubernetes_widget_wiring_holds_together():
    """Three pieces have to agree or the widget renders EMPTY rather than erroring.

    The config must ask for cluster mode, the pod must name the SA that mode authenticates with, and
    that SA must be able to read the metrics API. Any one of them missing looks identical from the
    dashboard — a tile with no numbers, which reads as "nothing to report".

    A fourth piece pairs with the RBAC rules rather than the widget: `ingress: false`. Upstream's
    `ingress-list.js` destructures `const { ingress = true }`, so `mode: cluster` alone makes the
    pod list `ingresses.networking.k8s.io` on every page load. This ClusterRole deliberately does
    not grant that read, so the two must move together — grant nothing, ask for nothing (#1428,
    #1459). Drop the key and the pod logs an RBAC denial per page load; grant the read instead and
    a read-only identity lists an empty set forever.
    """
    role = K8S / "homepage"
    kubernetes_config = yaml_fast.safe_load(
        (role / "templates" / "kubernetes.yaml.j2").read_text()
    )
    assert kubernetes_config["mode"] == "cluster"
    assert kubernetes_config["ingress"] is False

    deployment = yaml_fast.safe_load(
        _render(
            role / "templates" / "deployment.yaml.j2",
            container_item=next(c for c in _k8s_entries() if c["name"] == "homepage"),
            **_role_defaults("homepage"),
        )
    )
    assert deployment["spec"]["template"]["spec"]["serviceAccountName"] == "homepage"

    rbac = [
        d
        for d in yaml_fast.safe_load_all(
            _render(role / "templates" / "rbac.yaml.j2", **_role_defaults("homepage"))
        )
        if d
    ]
    rules = [rule for d in rbac if d["kind"] == "ClusterRole" for rule in d["rules"]]
    assert _grant_violations(rules) == []
    # Exact match, not `in`: see ansible/tests/repo/test_no_host_shaped_membership_literal.py
    assert any(
        g == "metrics.k8s.io" for rule in rules for g in rule.get("apiGroups", [])
    ), "no metrics.k8s.io read: every CPU/memory figure in the widget would be blank"
    assert not any(
        g == "networking.k8s.io" for rule in rules for g in rule.get("apiGroups", [])
    ), (
        "Ingress reads are granted, so `ingress: false` above is the wrong half of the pair"
    )


def test_readonly_role_covers_the_crd_groups_this_homelab_deploys():
    """`view` covers no CRDs and nothing aggregates into it, so a group missing from the
    list degrades silently: the kubeconfig still works, that one `kubectl get` says
    Forbidden, and the caller falls back to sudo — which is the thing this replaced."""
    groups = set(K3S_DEFAULTS["k3s_readonly_crd_api_groups"])
    route = (ANSIBLE / "templates" / "ingressroute.yml.j2").read_text()
    # Match the apiVersion line itself, not a bare substring: `traefik.io` appears in
    # comments and annotation keys too, so a substring check would keep passing after the
    # macro moved off the group.
    assert re.search(r"^apiVersion: traefik\.io/", route, re.MULTILINE), (
        "ingressroute macro no longer uses the traefik.io group"
    )
    # Exact match, not `in`: see ansible/tests/repo/test_no_host_shaped_membership_literal.py
    assert any(g == "traefik.io" for g in groups), (
        "IngressRoute/Middleware unreadable without sudo"
    )
