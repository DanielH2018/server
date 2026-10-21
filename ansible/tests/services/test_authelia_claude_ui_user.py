"""Authelia's users database holds three users, and one bug would make them the same account.

`claude-ui` is the identity the headless UI tier logs in as, so it can reach the
`two_factor` services (code-server, n8n, longhorn) without a code typed off a phone. Each
user's argon2 digest is its own SOPS key, `authelia_password_argon2` and its two siblings. A
template line that reads the wrong key hands two users one password, and nothing about that
presents as a failure. Both accounts log in, both render their UIs, and the operator's
password silently opens the tier that exists to be revocable on its own. An earlier
read-back of the live Secret had exactly that bug, anchoring on the first `$argon2` run in
the file whichever user it was resolving.

So the rules here are about the users staying distinct. Each is a
`..._is_clean` / `..._is_flagged` pair over a predicate, applied afterwards to the real
rendered manifests, with a non-vacuity assertion that the users database was found at all.
"""

import pytest
from lib import yaml_fast
from _helpers import K8S_ROLES
from _k8s_render import render_role_template, rendered_docs
from lib.render_context import render_context
from renderer.renderer_lib.placeholders import Sentinels, placeholder_vars

OPERATOR_PLACEHOLDER = "$argon2id$v=19$m=65536,t=3,p=4$operator"
CLAUDE_PLACEHOLDER = "$argon2id$v=19$m=65536,t=3,p=4$claudeui"
AGENT_PLACEHOLDER = "$argon2id$v=19$m=65536,t=3,p=4$claudeagent"


def users_database(docs):
    """The rendered `users_database.yml`, as a username -> user mapping.

    It is a YAML document nested inside a Secret's `stringData`, so it loads twice — the
    outer manifest, then the string it carries.
    """
    for role, _name, doc in docs:
        if role != "authelia" or not isinstance(doc, dict):
            continue
        raw = (doc.get("stringData") or {}).get("users_database.yml")
        if not raw:
            continue
        return (yaml_fast.safe_load(raw) or {}).get("users") or {}
    return {}


def digests_are_distinct(users):
    """True when no two users share a password digest."""
    digests = [u.get("password") for u in users.values()]
    return len(set(digests)) == len(digests)


def every_user_has_a_digest(users):
    """True when every user carries a non-empty argon2 hash.

    An empty one renders an account nobody can log into, which Authelia starts up with
    perfectly happily.
    """
    return bool(users) and all(
        str(u.get("password") or "").startswith("$argon2") for u in users.values()
    )


def test_distinct_digests_is_clean():
    assert digests_are_distinct(
        {
            "operator": {"password": OPERATOR_PLACEHOLDER},
            "claude-ui": {"password": CLAUDE_PLACEHOLDER},
        }
    )


def test_a_shared_digest_is_flagged():
    """The cross-wiring bug's exact signature: both users resolved to one hash."""
    assert not digests_are_distinct(
        {
            "operator": {"password": OPERATOR_PLACEHOLDER},
            "claude-ui": {"password": OPERATOR_PLACEHOLDER},
        }
    )


def test_populated_digests_are_clean():
    assert every_user_has_a_digest({"claude-ui": {"password": CLAUDE_PLACEHOLDER}})


def test_an_empty_digest_is_flagged():
    assert not every_user_has_a_digest({"claude-ui": {"password": ""}})


def test_no_users_at_all_is_flagged():
    assert not every_user_has_a_digest({})


def carries_none_of(user, privileged):
    """True when the user is in none of the `privileged` groups."""
    return not set(user.get("groups") or []) & set(privileged)


def test_a_user_outside_the_privileged_groups_is_clean():
    assert carries_none_of({"groups": []}, {"admins", "dev"})


def test_a_user_in_a_privileged_group_is_flagged():
    assert not carries_none_of({"groups": ["dev", "admins"]}, {"admins"})


ROLE = K8S_ROLES / "authelia"


@pytest.fixture(scope="module")
def live_users():
    """The rendered users database, with the non-vacuity check every rule below needs.

    Without it each rule passes over an empty mapping the moment the Secret key is renamed
    or the template stops rendering the block.

    The render harness stubs every SOPS value to the literal `STUB`, so this fixture can
    say who is in the database and what groups they carry, but nothing about their password
    digests — both render as the same stub. The digest rules render the template again with
    a distinct value in each user's digest key instead.
    """
    found = users_database(list(rendered_docs()))
    assert len(found) >= 2, (
        f"expected at least the operator and claude-ui in the rendered users database, "
        f"found {sorted(found)}"
    )
    return found


def test_the_claude_ui_user_is_rendered(live_users):
    assert "claude-ui" in live_users, (
        "the headless UI tier logs in as claude-ui; without this block its two_factor "
        f"services are unreachable. Found {sorted(live_users)}"
    )


def test_the_claude_agent_user_is_rendered_in_none_of_the_operators_groups(live_users):
    """Grafana maps `admins` to Admin and Headlamp binds `oidc:admins`; see the role defaults.

    The groups to keep it out of are the operator's own, read from the render, so a group
    added to the operator later is covered without editing this test.
    """
    agent = render_context(ROLE, strict=True)["authelia_k8s_agent_user"]
    assert agent in live_users, (
        f"the homelab-ui login the agent user holds needs this account; found "
        f"{sorted(live_users)}"
    )
    operator_groups = {
        g
        for name, u in live_users.items()
        if name != agent
        for g in u.get("groups") or []
    }
    assert "admins" in operator_groups, "the operator is no longer in `admins`"
    assert carries_none_of(live_users[agent], operator_groups), live_users[agent]


def test_the_claude_ui_user_carries_groups(live_users):
    """Its groups match the operator's on purpose — see the role defaults.

    The access_control rules are domain-scoped, so groups decide only what the apps behind
    those routes render. A narrower set would have the UI tier assert against a degraded
    page while still reporting green.
    """
    assert live_users["claude-ui"].get("groups")


def test_each_user_renders_the_digest_resolved_for_that_user():
    """Whether one hash fills both accounts, which the stubbed render cannot show.

    Each user's digest is its own SOPS key. Rendering with a different placeholder in each
    says which key reaches which account: a block reading the
    operator's variable gives both users one password, and a swap gives each the other's.
    """
    operator = "operator"
    claude = render_context(K8S_ROLES / "authelia", strict=True)[
        "authelia_k8s_claude_user"
    ]
    agent = render_context(K8S_ROLES / "authelia", strict=True)[
        "authelia_k8s_agent_user"
    ]
    secret = yaml_fast.safe_load(
        render_role_template(
            "authelia",
            "config-secret.yaml.j2",
            {
                "authelia_user": operator,
                "authelia_k8s_manage_claude_user": True,
                "authelia_k8s_manage_agent_user": True,
                "authelia_password_argon2": OPERATOR_PLACEHOLDER,
                "authelia_claude_password_argon2": CLAUDE_PLACEHOLDER,
                "authelia_agent_password_argon2": AGENT_PLACEHOLDER,
            },
        )
    )
    users = users_database([("authelia", "config-secret.yaml.j2", secret)])
    assert sorted(users) == sorted([operator, claude, agent]), (
        f"expected exactly {operator!r}, {claude!r} and {agent!r} in the users database, "
        f"found {sorted(users)}"
    )
    assert digests_are_distinct(users), (
        "both users render one password digest, so the operator's password opens the "
        "claude-ui tier"
    )
    assert users[operator]["password"] == OPERATOR_PLACEHOLDER, users
    assert users[claude]["password"] == CLAUDE_PLACEHOLDER, users
    assert users[agent]["password"] == AGENT_PLACEHOLDER, users


def test_a_cluster_that_does_not_manage_the_agent_user_renders_no_such_account():
    """A cluster without the agent user holds no digest for it, so the account must not render."""
    agent = render_context(K8S_ROLES / "authelia", strict=True)[
        "authelia_k8s_agent_user"
    ]
    secret = yaml_fast.safe_load(
        render_role_template(
            "authelia",
            "config-secret.yaml.j2",
            {
                "authelia_k8s_manage_agent_user": False,
                "authelia_password_argon2": OPERATOR_PLACEHOLDER,
                "authelia_claude_password_argon2": CLAUDE_PLACEHOLDER,
            },
        )
    )
    users = users_database([("authelia", "config-secret.yaml.j2", secret)])
    assert agent not in users and len(users) == 2, sorted(users)


# ── the TOTP seed (target slice D5, step 26) ────────────────────────────────────────────────

_TOTP_KEY = "authelia_claude_totp_secret"


def _placed_totp_keys(manage_claude):
    text = render_role_template(
        "authelia",
        "config-secret.yaml.j2",
        {
            **placeholder_vars({_TOTP_KEY}),
            "authelia_k8s_manage_claude_user": manage_claude,
        },
    )
    return Sentinels({_TOTP_KEY}).key_set({"config-secret.yaml": text})


_DIGEST_KEYS = {
    "authelia_password_argon2",
    "authelia_claude_password_argon2",
    "authelia_agent_password_argon2",
}


@pytest.mark.parametrize(
    ("manage_claude", "manage_agent"),
    [(True, True), (False, True), (True, False), (False, False)],
)
def test_the_placeholder_render_places_a_managed_users_digest_only(
    manage_claude, manage_agent
):
    """`cluster_vars.py write` checks a digest only when the placeholder render places it.

    The refusal succeeds the deploy-time digest assert, which checked each managed user's
    digest under the manage flags. The template must name each digest under the same flag.
    """
    flags = {
        "authelia_k8s_manage_claude_user": manage_claude,
        "authelia_k8s_manage_agent_user": manage_agent,
    }
    text = render_role_template(
        "authelia",
        "config-secret.yaml.j2",
        {**placeholder_vars(_DIGEST_KEYS), **flags},
    )
    placed = Sentinels(_DIGEST_KEYS).key_set({"config-secret.yaml": text})
    checked = {"authelia_password_argon2"}
    checked |= {"authelia_claude_password_argon2"} if manage_claude else set()
    checked |= {"authelia_agent_password_argon2"} if manage_agent else set()
    assert placed == checked


def test_the_placeholder_render_places_the_totp_secret_only_for_a_managed_claude_user():
    """`cluster_vars.py write` refuses an empty value only for a key the render places.

    That refusal succeeds the deploy-time "Fail if the claude-ui credentials are missing"
    assert, which checked the secret under `authelia_k8s_manage_claude_user`, so the render
    must place the TOTP secret under the same condition.
    """
    assert _placed_totp_keys(True) == {_TOTP_KEY}
    assert _placed_totp_keys(False) == set()


def test_the_totp_secret_never_stands_alone_in_a_field():
    """The manifests role's standalone-value guard reads a tag's whole map row.

    It ignores `authelia_k8s_manage_claude_user`. If the key stood alone, a cluster with the
    lever off would resolve it to the role default '', which the guard refuses, and every
    authelia deploy there would stop. The `|` block keeps it out of the row.
    """
    keys = Sentinels({_TOTP_KEY})
    raw = render_role_template(
        "authelia", "config-secret.yaml.j2", placeholder_vars({_TOTP_KEY})
    )
    assert keys.key_set({"config-secret.yaml": raw}) == {_TOTP_KEY}, (
        "the TOTP key is not rendered at all, so this test checks nothing"
    )
    assert keys.standalone_keys({"config-secret.yaml": keys.finish(raw)}) == frozenset()


def _seed_container(manage_claude):
    deployment = yaml_fast.safe_load(
        render_role_template(
            "authelia",
            "deployment.yaml.j2",
            {"authelia_k8s_manage_claude_user": manage_claude},
        )
    )
    inits = deployment["spec"]["template"]["spec"].get("initContainers") or []
    return [c for c in inits if c["name"] == "seed-claude-totp"]


def test_the_seed_init_container_reads_the_key_the_secret_renders():
    """The init container replaced an exec into the running pod, so it must find its input.

    A `secretKeyRef` naming a key the Secret does not carry leaves the pod in
    CreateContainerConfigError, which under Recreate is an SSO outage.
    """
    (seed,) = _seed_container(True)
    secret = yaml_fast.safe_load(
        render_role_template("authelia", "config-secret.yaml.j2")
    )
    refs = [
        e["valueFrom"]["secretKeyRef"]
        for e in seed["env"]
        if "secretKeyRef" in e.get("valueFrom", {})
    ]
    assert refs == [{"name": "authelia-config", "key": "claude-totp-secret"}], refs
    assert secret["metadata"]["name"] == "authelia-config"
    assert "claude-totp-secret" in secret["stringData"], sorted(secret["stringData"])


def test_the_seed_migrates_first_and_keeps_the_otpauth_uri_out_of_the_log():
    """`totp generate` refuses an outdated schema, and it prints the secret on stdout."""
    (seed,) = _seed_container(True)
    script = seed["command"][-1]
    migrate = script.index("authelia storage migrate up")
    generate = script.index("authelia storage user totp generate")
    assert migrate < generate, script
    assert "schema already up to date" in script[migrate:generate], script
    after = script[generate:]
    assert ">/dev/null" in after[: after.index("then")], script
