"""Authelia's users database holds two users, and one bug would make them the same account.

`claude-ui` is the identity the headless UI tier logs in as, so it can reach the
`two_factor` services (code-server, n8n, longhorn) without a code typed off a phone. The
role reads each user's existing argon2 digest back out of the live Secret before deciding
whether to mint a new one. A read-back that anchors on the hash itself —
`regex_search('\\$argon2[^\\s\\']+')` over the whole decoded file — is exact with one user.
With two it returns the FIRST digest whichever user is being resolved, which hands
both users one password — and nothing about that presents as a failure. Both accounts log
in, both render their UIs, and the operator's password silently opens the tier that exists
to be revocable on its own.

So the rules here are about the two users staying distinct. Each is a
`..._is_clean` / `..._is_flagged` pair over a predicate, applied afterwards to the real
rendered manifests, with a non-vacuity assertion that the users database was found at all.
"""

import pytest
from lib import yaml_fast
from _helpers import K8S_ROLES
from _k8s_render import host_context, render_role_template, rendered_docs
from validate.k8s_manifests import role_defaults

OPERATOR_PLACEHOLDER = "$argon2id$v=19$m=65536,t=3,p=4$operator"
CLAUDE_PLACEHOLDER = "$argon2id$v=19$m=65536,t=3,p=4$claudeui"


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
    """The read-back bug's exact signature: both users resolved to one hash."""
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


ROLE = K8S_ROLES / "authelia"


@pytest.fixture(scope="module")
def live_users():
    """The rendered users database, with the non-vacuity check every rule below needs.

    Without it each rule passes over an empty mapping the moment the Secret key is renamed
    or the template stops rendering the block.

    The render harness stubs every SOPS value to the literal `STUB`, so this fixture can
    say who is in the database and what groups they carry, but nothing about their password
    digests — both render as the same stub. The digest rules render the template again with
    a distinct value in each user's hash variable instead.
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


def test_the_claude_ui_user_carries_groups(live_users):
    """Its groups match the operator's on purpose — see the role defaults.

    The access_control rules are domain-scoped, so groups decide only what the apps behind
    those routes render. A narrower set would have the UI tier assert against a degraded
    page while still reporting green.
    """
    assert live_users["claude-ui"].get("groups")


def test_each_user_renders_the_digest_resolved_for_that_user():
    """Whether one hash fills both accounts, which the stubbed render cannot show.

    The role resolves each user's digest into its own variable. Rendering with a different
    placeholder in each says which variable reaches which account: a block reading the
    operator's variable gives both users one password, and a swap gives each the other's.
    """
    operator = "operator"
    claude = role_defaults("authelia", host_context())["authelia_k8s_claude_user"]
    secret = yaml_fast.safe_load(
        render_role_template(
            "authelia",
            "config-secret.yaml.j2",
            {
                "authelia_user": operator,
                "authelia_k8s_manage_claude_user": True,
                "authelia_password_hash": OPERATOR_PLACEHOLDER,
                "authelia_claude_password_hash": CLAUDE_PLACEHOLDER,
            },
        )
    )
    users = users_database([("authelia", "config-secret.yaml.j2", secret)])
    assert sorted(users) == sorted([operator, claude]), (
        f"expected exactly {operator!r} and {claude!r} in the users database, "
        f"found {sorted(users)}"
    )
    assert digests_are_distinct(users), (
        "both users render one password digest, so the operator's password opens the "
        "claude-ui tier"
    )
    assert users[operator]["password"] == OPERATOR_PLACEHOLDER, users
    assert users[claude]["password"] == CLAUDE_PLACEHOLDER, users


def test_the_hash_read_back_is_keyed_by_username():
    """The regression this file's docstring describes, at its source.

    A read-back that anchors on `$argon2` rather than on the username returns the first
    digest in the file for whichever user it is resolving. Both accounts then work, so only
    a source check catches it.
    """
    tasks = (ROLE / "tasks" / "main.yml").read_text()
    assert "regex_search('\\$argon2[^\\s\\']+')" not in tasks, (
        "the users-database read-back is matching the first $argon2 run in the file again; "
        "it must be keyed by username, or both users get one password"
    )
    assert "authelia_k8s_existing_users.get(authelia_user" in tasks
    assert "authelia_k8s_existing_users.get(authelia_k8s_claude_user" in tasks
