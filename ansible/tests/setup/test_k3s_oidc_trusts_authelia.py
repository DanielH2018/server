"""The API server's OIDC trust in Authelia, and the names it has to keep in step.

Headlamp does not authorise anything under OIDC: it forwards the browser's `id_token` and the
API SERVER accepts or rejects it. So the trust lives in `roles/setup/k3s`, one role away from
the workload that needs it, and its values are only correct relative to something outside this
role — the Authelia issuer hosts, the Authelia client id, and the group name
`roles/k8s/headlamp` binds. Each of those drifts silently: the login succeeds and the dashboard
renders an empty cluster, with nothing in any log.

The trust is an `AuthenticationConfiguration` file, not six `--kube-apiserver-arg=oidc-*`
flags, because Authelia mints a different `iss` per hostname and the flag takes one value.
That is why this file checks a rendered YAML document rather than an argument string, and
why two of the checks below are about the flags NOT being there.

The document comes from `_setup_render`, the harness `validate/setup_templates.py` renders the
setup plane with, so the layering under it cannot drift from what that gate renders. It used to
come from a context assembled here — the role's defaults, group_vars, and two keys re-rendered
by hand because they are themselves templates over `domain` (#3202).

See `roles/setup/k3s/defaults/main.yml` and `templates/authentication-config.yaml.j2` for why
each value is what it is.
"""

import re

import jinja2
from lib import yaml_fast
from lib.ansible_jinja_env import make_ansible_env

from _helpers import ANSIBLE, load_defaults
from lib.repo_paths import ALL_VARS
from _k8s_render import rendered_docs
from _setup_render import render_setup_text

K3S = ANSIBLE / "roles" / "setup" / "k3s"
HEADLAMP = ANSIBLE / "roles" / "k8s" / "headlamp"

# The two hostnames Authelia answers on, and the two `iss` values they mint. Measured
# against `/.well-known/openid-configuration` on each name. Named rather than
# counted: a count of 2 passes while one of them has been swapped for something else, and the
# whole point of the file is that BOTH are trusted.
REQUIRED_ISSUER_HOSTS = frozenset(
    {
        "auth.local.example.test",
        "auth.example.test",
    }
)

# The flags the config file replaced. kube-apiserver refuses to start when a JWT-configuring
# `--authentication-config` is passed alongside any of these, and on this Type=notify unit a
# refusal to start is an indefinite hang rather than an error.
MUTUALLY_EXCLUSIVE_FLAGS = frozenset(
    {
        "oidc-issuer-url",
        "oidc-client-id",
        "oidc-username-claim",
        "oidc-username-prefix",
        "oidc-groups-claim",
        "oidc-groups-prefix",
        "oidc-signing-algs",
        "oidc-ca-file",
        "oidc-required-claim",
    }
)


def _all_vars() -> dict:
    return yaml_fast.safe_load(ALL_VARS.read_text())


def _env() -> jinja2.Environment:
    return make_ansible_env(undefined_cls=jinja2.StrictUndefined)


# `domain` is a SOPS value no test can read, and only the issuer URLs interpolate it. Every
# assertion here is about a URL's shape rather than the estate's real hostname, so the render
# gets a stand-in and REQUIRED_ISSUER_HOSTS above names the hosts it produces.
DOMAIN = "example.test"


def _context() -> dict:
    """The role's own variables, with a stand-in domain, for the server-args render.

    `k3s_server_args` is a defaults VALUE rather than a template file, so it is rendered here
    rather than through the harness. `_setup_render.role_context` resolves each key the way
    Ansible does, which is what used to be done by hand for the two `domain`-derived paths.
    """
    defaults = load_defaults(K3S)
    context = {
        "server_ip": "10.0.0.215",
        "domain": DOMAIN,
        **{k: v for k, v in defaults.items() if k != "k3s_server_args"},
        **_all_vars(),
    }
    env = _env()
    for key in ("k3s_audit_log_path", "k3s_audit_log_dir"):
        value = context.get(key)
        if isinstance(value, str) and "{{" in value:
            context[key] = env.from_string(value).render(**context)
    return context


def _rendered_server_args(context: dict | None = None) -> str:
    """`k3s_server_args` as the installer receives it."""
    context = context or _context()
    return _env().from_string(load_defaults(K3S)["k3s_server_args"]).render(**context)


def _rendered_auth_config() -> dict:
    """`authentication-config.yaml.j2` as the apiserver parses it."""
    return yaml_fast.safe_load(
        render_setup_text("k3s", "authentication-config.yaml.j2", {"domain": DOMAIN})
    )


def _apiserver_args(args: str) -> dict[str, str]:
    """The `--kube-apiserver-arg=` values in an argument string, as name -> value."""
    return dict(re.findall(r"--kube-apiserver-arg=([a-z0-9-]+)=(\S+)", args))


def _issuer_hosts(config: dict) -> set[str]:
    """The host of every trusted issuer URL. Empty means nothing is trusted."""
    return {
        url.removeprefix("https://")
        for entry in config.get("jwt") or []
        if (url := entry.get("issuer", {}).get("url", ""))
    }


def _username_prefixes_that_prefix(config: dict) -> list[str]:
    """Every username prefix that would actually prefix a username. Empty means disabled.

    `""` is the only value that disables prefixing under the structured config. A missing key
    is not in scope here — the apiserver rejects the file outright, so its own test asserts
    presence rather than folding it into this list.
    """
    return [
        prefix
        for entry in config.get("jwt") or []
        if (prefix := entry.get("claimMappings", {}).get("username", {}).get("prefix"))
    ]


def _missing_issuers(config: dict) -> list[str]:
    """Every required issuer host absent from the config. Empty means both are trusted."""
    return sorted(REQUIRED_ISSUER_HOSTS - _issuer_hosts(config))


def test_the_apiserver_is_pointed_at_the_authentication_config_file():
    """One flag, and it must name the path the role actually writes.

    A path mismatch is the worst failure available here: the apiserver refuses to start on a
    missing authentication config, and the k3s unit is Type=notify with TimeoutStartSec=0, so
    the restart hangs indefinitely rather than failing the play.
    """
    args = _apiserver_args(_rendered_server_args())
    assert (
        args.get("authentication-config")
        == load_defaults(K3S)["k3s_authentication_config_path"]
    )


def test_the_oidc_flags_are_gone_now_that_a_config_file_configures_jwt():
    """The two mechanisms are mutually exclusive, not additive.

    "This flag is mutually exclusive with the --oidc-* flags if the file configures the JWT
    Token authenticator" (kube-apiserver flag reference). Passing both makes the apiserver
    refuse to start, which on this unit is the indefinite hang above. A re-added flag reads as
    a harmless belt-and-braces edit, which is exactly why this is a test.
    """
    present = set(_apiserver_args(_rendered_server_args()))
    assert present & MUTUALLY_EXCLUSIVE_FLAGS == set()


def test_the_config_declares_the_stable_authentication_config_schema():
    """A v1beta1 apiVersion parses and is then rejected by a v1-only apiserver, and the
    rejection is a refusal to start rather than a warning."""
    config = _rendered_auth_config()
    assert config["apiVersion"] == "apiserver.config.k8s.io/v1"
    assert config["kind"] == "AuthenticationConfiguration"


def test_both_authelia_issuers_are_trusted():
    """The reason this is a file rather than a flag.

    Authelia derives `iss` from the host the request arrived on, so Headlamp's two routes mint
    two different issuers. Trusting only one of them is the failure: the login completes
    at Authelia and the API server refuses the token.
    """
    assert _missing_issuers(_rendered_auth_config()) == []


def test_the_issuer_check_rejects_a_config_that_trusts_only_the_lan_name():
    """The rejecting half, and it reproduces the specific defect rather than a generic one.

    Without it the check above passes on a config with no `jwt` entries at all — a set
    difference against nothing is empty — and passes just as happily once `_issuer_hosts` has
    stopped matching the document's shape.
    """
    config = _rendered_auth_config()
    config["jwt"] = [
        entry for entry in config["jwt"] if "auth.local." in entry["issuer"]["url"]
    ]
    assert _missing_issuers(config) == ["auth.example.test"]


def test_every_issuer_url_is_exact_and_distinct():
    """`iss` is compared exactly, with no normalisation, and duplicate issuers fail the
    apiserver's own validation."""
    urls = [entry["issuer"]["url"] for entry in _rendered_auth_config()["jwt"]]
    assert len(urls) == len(set(urls)), f"duplicate issuer URLs: {urls}"
    for url in urls:
        assert url.startswith("https://"), f"{url} is not https; validation requires it"
        assert not url.endswith("/"), (
            f"{url} has a trailing slash; the `iss` claim does not, and the comparison is exact"
        )


def test_every_issuer_names_the_client_id_as_its_audience():
    """Authelia's default `id_token_audience_mode: specification` puts only the client id in
    `aud`, and `audiences` is required and must be non-empty."""
    client_id = load_defaults(K3S)["k3s_oidc_client_id"]
    for entry in _rendered_auth_config()["jwt"]:
        assert entry["issuer"]["audiences"] == [client_id], entry["issuer"]["url"]


def test_username_prefixing_is_disabled_with_an_empty_string_not_a_hyphen():
    """The one field whose meaning differs from the flags it replaces.

    `--oidc-username-prefix=-` was a sentinel meaning "no prefix", because the flag path
    prefixed the username with the issuer URL plus `#` for any claim other than `email`. The
    structured config does no implicit prefixing and honours no sentinel: `prefix` is required
    whenever `claim` is set, and `""` is what disables it. Carrying the `-` across would
    produce usernames literally prefixed with a hyphen — subjects that read as a bug in every
    audit log and that `kubectl auth can-i --as=<user>` cannot test.
    """
    config = _rendered_auth_config()
    for entry in config["jwt"]:
        username = entry["claimMappings"]["username"]
        assert username["claim"] == load_defaults(K3S)["k3s_oidc_username_claim"]
        assert "prefix" in username, (
            "prefix is required when claim is set; the apiserver rejects the file without it"
        )
    assert _username_prefixes_that_prefix(config) == []


def test_the_username_prefix_check_rejects_the_flag_paths_old_sentinel():
    """The rejecting half, and it reproduces the specific mistake this move invites.

    `-` is the value the six flags carried, so copying it across is the likely edit rather
    than a hypothetical one — and it reads as correct to anyone who remembers the flag. It
    produces usernames literally prefixed with a hyphen. The check must flag it, and must
    equally flag an ordinary prefix, or it is only testing that `""` equals itself.
    """
    for prefix in ("-", "oidc:", "https://auth.example.test#"):
        config = _rendered_auth_config()
        for entry in config["jwt"]:
            entry["claimMappings"]["username"]["prefix"] = prefix
        flagged = _username_prefixes_that_prefix(config)
        assert flagged == [prefix] * len(config["jwt"]), prefix


def test_groups_prefix_matches_the_group_headlamp_binds():
    """Two roles, one string, and neither fails loudly when they disagree.

    The API server applies the groups prefix to every value of the claim before RBAC sees it;
    `roles/k8s/headlamp` binds the prefixed name. A mismatch is a login that succeeds onto an
    empty dashboard.
    """
    headlamp = yaml_fast.safe_load((HEADLAMP / "defaults" / "main.yml").read_text())
    group = headlamp["headlamp_k8s_oidc_group"]
    for entry in _rendered_auth_config()["jwt"]:
        groups = entry["claimMappings"]["groups"]
        assert groups["claim"] == load_defaults(K3S)["k3s_oidc_groups_claim"]
        prefix = groups["prefix"]
        assert group.startswith(prefix), (
            f"{group!r} does not carry the prefix {prefix!r}"
        )
        assert group != prefix, "the prefix alone names no Authelia group"
        assert not group.removeprefix(prefix).startswith("system:"), (
            f"{group!r} would let an Authelia group impersonate a built-in one"
        )


def test_headlamp_sends_logins_to_an_issuer_the_apiserver_trusts():
    """Headlamp reads its issuer ONCE at start and uses it for every request, so this single
    value decides where both routes' logins go. An issuer the apiserver does not trust is a
    login that completes at Authelia and is then refused — with the dashboard rendered and
    every call Forbidden.
    """
    headlamp = yaml_fast.safe_load((HEADLAMP / "defaults" / "main.yml").read_text())
    issuer = (
        _env()
        .from_string(headlamp["headlamp_k8s_oidc_issuer_url"])
        .render(domain=DOMAIN)
    )
    assert issuer.removeprefix("https://") in _issuer_hosts(_rendered_auth_config())


def _authelia_client_ids() -> set[str]:
    """Every `client_id` in the rendered Authelia config.

    The config is a YAML document inside a Secret's `stringData`, so it is loaded twice.
    """
    for role, _name, doc in rendered_docs():
        if role != "authelia" or not isinstance(doc, dict):
            continue
        raw = (doc.get("stringData") or {}).get("configuration.yml")
        if not raw:
            continue
        oidc = (yaml_fast.safe_load(raw).get("identity_providers") or {}).get("oidc")
        return {client["client_id"] for client in (oidc or {}).get("clients") or []}
    return set()


def _headlamp_client_id_arg() -> str | None:
    """The `-oidc-client-id=` value the rendered Headlamp Deployment passes, or None."""
    for role, _name, doc in rendered_docs():
        if role != "headlamp" or not isinstance(doc, dict):
            continue
        if doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            for arg in container.get("args") or []:
                if arg.startswith("-oidc-client-id="):
                    return arg.removeprefix("-oidc-client-id=")
    return None


def _client_id_disagreements(
    headlamp_id: str,
    enabled: bool,
    deployment_arg: str | None,
    authelia_ids: set[str],
    audiences: list[list[str]],
) -> list[str]:
    """Every place the headlamp client id is missing or different. Empty means they agree.

    `headlamp_id` is `headlamp_k8s_oidc_client_id` and `enabled` is
    `headlamp_k8s_oidc_enabled`. `deployment_arg` is what the rendered Deployment passes, which
    is None when the switch is off: that is the documented rollback, under which the Authelia
    client still renders and is still compared.
    `audiences` is one list per trusted issuer, because each `jwt` entry carries its own.
    """
    problems = []
    if enabled and deployment_arg != headlamp_id:
        problems.append(f"the Deployment passes -oidc-client-id={deployment_arg}")
    if headlamp_id not in authelia_ids:
        problems.append(
            f"Authelia has no client {headlamp_id!r}: {sorted(authelia_ids)}"
        )
    problems += [
        f"an issuer's audiences are {entry}, not [{headlamp_id!r}]"
        for entry in audiences
        if entry != [headlamp_id]
    ]
    return problems


def test_headlamp_client_id_agrees_across_authelia_headlamp_and_k3s():
    """One client id, written in three roles, and no disagreement among them errors.

    Headlamp sends it as `client_id`, Authelia must hold a client by that name, and Authelia
    puts it in `aud`, which the API server matches against each issuer's `audiences`. The
    first mismatch fails the login at Authelia, the second passes the login and Forbids every
    call. The ids are kept as three literals rather than one shared variable because
    `roles/setup/k3s` is applied by hand and restarts the control plane; this test is the
    tie between them (#3751).
    """
    audiences = [e["issuer"]["audiences"] for e in _rendered_auth_config()["jwt"]]
    assert audiences, "the authentication config trusts no issuer; nothing was compared"
    headlamp = load_defaults(HEADLAMP)
    problems = _client_id_disagreements(
        headlamp["headlamp_k8s_oidc_client_id"],
        headlamp["headlamp_k8s_oidc_enabled"],
        _headlamp_client_id_arg(),
        _authelia_client_ids(),
        audiences,
    )
    assert problems == []


def test_the_client_id_check_flags_each_copy_that_drifts():
    """The rejecting half: renaming any one of the copies is flagged.

    Without it the test above passes on a check that returns `[]` for everything, and it
    passes too once `_authelia_client_ids` stops finding the client list.
    """
    assert "headlamp" in _authelia_client_ids()
    ids = {"grafana", "headlamp"}
    both = [["headlamp"], ["headlamp"]]
    assert _client_id_disagreements("headlamp", True, "headlamp", ids, both) == []
    assert len(_client_id_disagreements("dashboard", True, "dashboard", ids, both)) == 3
    assert len(_client_id_disagreements("headlamp", True, "dashboard", ids, both)) == 1
    assert len(_client_id_disagreements("headlamp", True, None, ids, both)) == 1
    assert (
        len(_client_id_disagreements("headlamp", True, "headlamp", {"grafana"}, both))
        == 1
    )
    assert _client_id_disagreements("headlamp", False, None, ids, both) == []
    one_off = [["headlamp"], ["x"]]
    assert (
        len(_client_id_disagreements("headlamp", True, "headlamp", ids, one_off)) == 1
    )
