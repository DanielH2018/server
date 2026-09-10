"""Headlamp's OIDC login: the branch, the flags, and the two hostnames it serves.

Headlamp does not authorise anything under OIDC — it forwards the browser's `id_token` and the
API SERVER decides — so every value here is correct only relative to something outside this
role. `ansible/tests/setup/test_k3s_oidc_trusts_authelia.py` owns the API server's half.

Split out of `test_k8s_manifests_rbac.py`, which owns the RBAC grants these logins land on.
"""

from lib import yaml_fast
from _manifest_guards import (
    K8S,
    _k8s_entries,
    _render,
    _role_defaults,
)


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
# one is never an error: no `-oidc-scopes=...groups` and the RBAC group subject matches
# nothing, no `-oidc-use-pkce` and the exchange fails against a client that requires PKCE.
# `-oidc-callback-url` is deliberately NOT here. It renders only when
# `headlamp_k8s_oidc_callback_url` is set, and the default leaves it empty so Headlamp derives
# the callback from each incoming request — which is what lets one instance serve both the LAN
# and the public hostname. See test_headlamp_leaves_the_oidc_callback_to_the_request_host.
OIDC_ARG_NAMES = frozenset(
    {
        "-oidc-client-id",
        "-oidc-idp-issuer-url",
        "-oidc-scopes",
        "-oidc-use-pkce",
    }
)


def _oidc_arg_names(args: list[str]) -> set[str]:
    return {arg.split("=", 1)[0] for arg in args if arg.startswith("-oidc-")}


def test_headlamp_with_oidc_off_browses_as_its_serviceaccount():
    """The fallback branch: no OIDC flags at all, the SA-token flag present.

    `headlamp_k8s_oidc_enabled` is passed explicitly rather than left to the role default,
    which is what this test did until the default was armed (#1390 part 3, 2026-09-10) and
    the assertion then read the ON branch. Both halves of the pair pin their own branch, so
    neither tracks whichever default happens to be set.
    """
    args = _headlamp_pod_spec(headlamp_k8s_oidc_enabled=False)["containers"][0]["args"]
    assert "-unsafe-use-service-account-token" in args
    assert _oidc_arg_names(args) == set()


def test_headlamp_oidc_is_armed_by_default():
    """The role default is ON, so a revert to the SA-token identity fails here.

    The pair above passes the flag explicitly and therefore cannot see the default move. A
    default silently flipped back would leave every OIDC assertion green while the deployed
    dashboard browsed as its ServiceAccount again — the disarmed-behind-a-green-test shape
    this repo has paid for. Arming depends on the `--kube-apiserver-arg=oidc-*` flags in
    roles/setup/k3s, applied by hand through k3s-bringup.yml; turn both off together.
    """
    assert _role_defaults("headlamp")["headlamp_k8s_oidc_enabled"] is True


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


def test_headlamp_sends_logins_to_the_public_authelia_name():
    """Headlamp reads its issuer ONCE at server start and uses that one provider for every
    request (`oidcAuthConfig.IdpIssuerURL`, backend/cmd/headlamp.go at v0.45.0), so this single
    value decides where BOTH routes' logins go. The LAN name sent public-route logins to a host
    an off-LAN browser cannot resolve, which is the defect this pins against; the public name
    is reachable from both sides. `roles/setup/k3s` trusts both issuers, so the choice is about
    which portal a browser is sent to, not which token the API server accepts.
    """
    args = _headlamp_pod_spec(headlamp_k8s_oidc_enabled=True)["containers"][0]["args"]
    flags = dict(a.split("=", 1) for a in args if a.startswith("-oidc-") and "=" in a)
    issuer = flags["-oidc-idp-issuer-url"]
    assert not issuer.startswith("https://auth.local."), (
        f"{issuer} is the LAN portal; an off-LAN browser cannot resolve it"
    )
    assert issuer.startswith("https://auth."), issuer
    assert not issuer.endswith("/"), (
        f"{issuer} has a trailing slash; the `iss` claim does not, and the API server's"
        " comparison is exact"
    )


def test_headlamp_leaves_the_oidc_callback_to_the_request_host():
    """The flag is ABSENT by default, and absent is the working state.

    Headlamp builds the callback from the incoming request when the config value is blank —
    `getOidcCallbackURL` reads the request host and `X-Forwarded-Proto` — so one instance
    sends the right `redirect_uri` on each of its two hostnames. Pinning it is what broke the
    public route's second half: every login was sent to the LAN callback. Omitting the flag is
    not the same as passing it empty, which is why this asserts absence.
    """
    args = _headlamp_pod_spec(headlamp_k8s_oidc_enabled=True)["containers"][0]["args"]
    assert not [a for a in args if a.startswith("-oidc-callback-url")]
    assert _role_defaults("headlamp")["headlamp_k8s_oidc_callback_url"] == ""


def test_the_oidc_callback_flag_still_renders_when_it_is_pinned():
    """The rejecting half of the pair above.

    Without it, the absence assertion passes just as happily on a template that has lost the
    flag entirely — an operator who pins the callback to pin one hostname would get a value
    silently ignored. The branch has to still work; the default just does not use it.
    """
    args = _headlamp_pod_spec(
        headlamp_k8s_oidc_enabled=True,
        headlamp_k8s_oidc_callback_url="https://headlamp.local.example.test/oidc-callback",
    )["containers"][0]["args"]
    assert (
        "-oidc-callback-url=https://headlamp.local.example.test/oidc-callback" in args
    )


def test_headlamp_keeps_its_serviceaccount_token_mounted():
    """The flag that removes the token prompt reads the projected SA token.

    Setting automountServiceAccountToken false — or omitting serviceAccountName, which silently
    falls back to the namespace `default` SA with no permissions — leaves a dashboard that loads,
    authenticates nobody, and shows an empty cluster.

    Asserted in BOTH branches, because the mount is not the SA-token flag's to own: `-in-cluster`
    reads the API server address and the cluster CA from that same projected mount and errors
    without it, so the mount has to survive OIDC removing the flag. The SA-token flag itself is
    pinned by the off-branch test, not here.
    """
    for enabled in (False, True):
        spec = _headlamp_pod_spec(headlamp_k8s_oidc_enabled=enabled)
        assert spec["serviceAccountName"] == "headlamp", enabled
        assert spec["automountServiceAccountToken"] is True, enabled
