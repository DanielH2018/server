"""The API server's OIDC trust in Authelia, and the two names it has to keep in step.

Headlamp does not authorise anything under OIDC: it forwards the browser's `id_token` and the
API SERVER accepts or rejects it. So the trust lives in `k3s_server_args`, one role away from
the workload that needs it, and three of its six values are only correct relative to something
outside this role — the Authelia issuer host, the Authelia client id, and the group name
`roles/k8s/headlamp` binds. Each of those drifts silently: the login succeeds and the dashboard
renders an empty cluster, with nothing in any log.

See `roles/setup/k3s/defaults/main.yml` for why each flag has the value it has.
"""

import re

import jinja2
from lib import yaml_fast

from _helpers import ANSIBLE

K3S = ANSIBLE / "roles" / "setup" / "k3s"
HEADLAMP = ANSIBLE / "roles" / "k8s" / "headlamp"

# The six flags, named rather than counted. A count passes while the set has been swapped for
# a different six, and each of these has its own failure mode: without `oidc-username-prefix`
# every username silently carries the issuer URL, without `oidc-groups-claim` the RBAC binding
# in roles/k8s/headlamp matches nothing.
REQUIRED_OIDC_FLAGS = frozenset(
    {
        "oidc-issuer-url",
        "oidc-client-id",
        "oidc-username-claim",
        "oidc-username-prefix",
        "oidc-groups-claim",
        "oidc-groups-prefix",
    }
)


def _defaults() -> dict:
    return yaml_fast.safe_load((K3S / "defaults" / "main.yml").read_text())


def _all_vars() -> dict:
    return yaml_fast.safe_load(
        (ANSIBLE / "inventory" / "group_vars" / "all.yml").read_text()
    )


def _rendered_server_args() -> str:
    """`k3s_server_args` as the installer receives it, with a stand-in domain.

    `domain` is a SOPS value no test can read; only the issuer argument interpolates it, and
    every assertion below is about the argument's shape rather than its host.
    """
    defaults = _defaults()
    context = {
        "server_ip": "10.0.0.215",
        "domain": "example.test",
        **{k: v for k, v in defaults.items() if k != "k3s_server_args"},
        **_all_vars(),
    }
    env = jinja2.Environment(
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        keep_trailing_newline=True,
    )
    for key in ("k3s_audit_log_path", "k3s_audit_log_dir"):
        value = context.get(key)
        if isinstance(value, str) and "{{" in value:
            context[key] = env.from_string(value).render(**context)
    return env.from_string(defaults["k3s_server_args"]).render(**context)


def _oidc_args(args: str) -> dict[str, str]:
    """The `oidc-*` apiserver arguments in an argument string, as name -> value."""
    return dict(re.findall(r"--kube-apiserver-arg=(oidc-[a-z-]+)=(\S+)", args))


def _missing_or_empty(args: str) -> list[str]:
    """Every required OIDC flag that is absent or carries no value. Empty means configured."""
    present = _oidc_args(args)
    return sorted(
        flag for flag in REQUIRED_OIDC_FLAGS if not present.get(flag, "").strip()
    )


def test_the_apiserver_is_given_every_oidc_flag():
    """A partial set is the worst outcome: the API server starts, trusts the issuer, and then
    authorises against a claim nobody configured."""
    assert _missing_or_empty(_rendered_server_args()) == []


def test_the_oidc_flag_check_rejects_an_argument_string_that_drops_one():
    """The rejecting half. Without it the check above passes on an argument string with no
    `oidc-*` flags at all, because `all(...)` over an empty set is true — and it passes just as
    happily on a regex that stopped matching.
    """
    args = _rendered_server_args()
    stripped = args.replace("--kube-apiserver-arg=oidc-groups-prefix=oidc:", "")
    assert _missing_or_empty(stripped) == ["oidc-groups-prefix"]


def test_oidc_username_prefix_is_disabled_rather_than_left_default():
    """kube-apiserver prefixes the username with the issuer URL plus `#` for any claim other
    than `email` (pkg/kubeapiserver/options/authentication.go). Left at the default, every
    subject in every audit log reads `https://auth.local.<domain>#daniel`, and
    `kubectl auth can-i --as=<user>` tests a subject that does not exist.
    """
    assert _oidc_args(_rendered_server_args())["oidc-username-prefix"] == "-"


def test_oidc_groups_prefix_matches_the_group_headlamp_binds():
    """Two roles, one string, and neither fails loudly when they disagree.

    The API server applies `oidc-groups-prefix` to every value of the groups claim before RBAC
    sees it; `roles/k8s/headlamp` binds the prefixed name. A mismatch is a login that succeeds
    onto an empty dashboard.
    """
    prefix = _oidc_args(_rendered_server_args())["oidc-groups-prefix"]
    headlamp = yaml_fast.safe_load((HEADLAMP / "defaults" / "main.yml").read_text())
    group = headlamp["headlamp_k8s_oidc_group"]
    assert group.startswith(prefix), f"{group!r} does not carry the prefix {prefix!r}"
    assert group != prefix, "the prefix alone names no Authelia group"
    assert not group.removeprefix(prefix).startswith("system:"), (
        f"{group!r} would let an Authelia group impersonate a built-in one"
    )


def test_oidc_issuer_url_pins_the_lan_authelia_name():
    """Authelia's `iss` follows the host the authorization request arrived on — measured
    2026-09-10, the discovery document returns `auth.local.<domain>` on the LAN name and
    `auth.<domain>` on the public one. This flag takes one value, and the Headlamp client's
    redirect_uris are LAN-only to match it.
    """
    issuer = _oidc_args(_rendered_server_args())["oidc-issuer-url"]
    assert issuer.startswith("https://auth.local."), issuer
    assert not issuer.endswith("/"), (
        f"{issuer} has a trailing slash; the `iss` claim does not, and the comparison is exact"
    )
