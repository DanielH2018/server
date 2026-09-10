"""Authelia's rendered configuration carries no key the pinned release does not know.

Authelia refuses to START on an unrecognised configuration key — it does not warn and carry on
— and the portal rolls under `Recreate` in front of most public routes, so the old pod is
already gone when the new one exits. Nothing between the editor and the SSO gate could see that
before this guard (#1608). `validate/k8s_manifests.py` renders the Secret and parses it as YAML,
where a bad key parses fine. `--dry-run` hands the Secret to the API server, which validates the
Secret and never looks inside `stringData`. `prek` runs the same render. Two live instances of
the class had already been paid for: `disable_startup_check` under `notifier.smtp` (#1464) and
the `webauthn` key set (#1502), each answered with its own hand-written frozenset of legal keys
— textual guards standing in for a parser nobody ran.

Authelia publishes a JSON Schema of its own configuration, per minor release, with
`additionalProperties: false` throughout. That is the parser's key set as data, so this guard
replaces the per-area frozensets with one check that needs no cluster, no credentials and no
container runtime. The schema is vendored under `scripts/validate/schemas/authelia.com/` for the
same reason the CRD schemas are: a hook that resolves DNS fails when DNS is down, and this repo
IS the DNS. Refresh it with `uv run python scripts/validate/refresh_crd_schemas.py`.

ONLY UNKNOWN KEYS ARE ASSERTED, not values, and that narrowing is deliberate rather than
timidity. Measured against the real render on 2026-09-10, whole-document validation reports five
errors and not one is a defect this guard should fail on: four are the `STUB` stand-ins the test
render substitutes for SOPS secrets (an OIDC client secret and a JWKS key are checked against
hash and PEM patterns), and the fifth is `notifier.smtp.sender`, which the schema's own `oneOf`
matches under both branches. A sixth, `authentication_backend.file.password.algorithm:
argon2id`, is a value the running 4.39.21 binary accepts and this schema's enum does not — the
schema is stricter than the parser on values, which is exactly the direction that would make a
value-level gate fail on a working config. Unknown keys have no such gap: the key set is what
Authelia rejects on, and it is what this file reads.
"""

import copy
import json

import pytest
from _helpers import REPO
from _k8s_render import rendered_docs
from lib import yaml_fast

jsonschema = pytest.importorskip("jsonschema")

SCHEMA_MINOR = "v4.39"
SCHEMA_PATH = (
    REPO / f"scripts/validate/schemas/authelia.com/configuration_{SCHEMA_MINOR}.json"
)
AUTHELIA_DEFAULTS = REPO / "ansible/roles/k8s/authelia/defaults/main.yml"

# Named members the vendored schema must define. A schema that downloaded as an error page, or
# whose `$defs` were restructured upstream, would otherwise leave every check below validating
# against nothing and passing. Two of these are the areas that produced #1464 and #1502.
REQUIRED_DEFS = frozenset(
    {"Configuration", "WebAuthn", "NotifierSMTP", "Session", "SessionRedis"}
)


@pytest.fixture(scope="module")
def schema():
    return json.loads(SCHEMA_PATH.read_text())


@pytest.fixture(scope="module")
def validator(schema):
    return jsonschema.Draft202012Validator(schema)


@pytest.fixture(scope="module")
def rendered_config():
    """The `configuration.yml` Authelia actually receives, parsed from the rendered Secret."""
    for role, _tpl, doc in rendered_docs():
        if role != "authelia" or doc.get("kind") != "Secret":
            continue
        if (doc.get("metadata") or {}).get("name") != "authelia-config":
            continue
        raw = (doc.get("stringData") or {}).get("configuration.yml")
        parsed = yaml_fast.safe_load(raw or "")
        assert parsed, (
            "the rendered authelia-config Secret carries no configuration.yml, so every "
            "assertion below would validate an empty document and pass"
        )
        return parsed
    pytest.fail("no rendered authelia-config Secret")


# --- the rule, as a predicate -------------------------------------------------------


def _walk(error):
    """Every error in the tree, including those nested under a `oneOf`/`anyOf` branch."""
    yield error
    for sub in error.context or ():
        yield from _walk(sub)


def unknown_config_keys(document, validator):
    """The paths of every key the schema does not declare. Empty when the config is clean."""
    found = []
    for error in validator.iter_errors(document):
        for sub in _walk(error):
            if sub.validator == "additionalProperties":
                found.append(("/".join(str(p) for p in sub.absolute_path), sub.message))
    return found


# --- the red proof ------------------------------------------------------------------


def test_a_config_with_no_unknown_keys_is_clean(rendered_config, validator):
    assert unknown_config_keys(rendered_config, validator) == []


def test_an_unknown_webauthn_key_is_flagged(rendered_config, validator):
    """#1502's area. `webauthn` is nested, so this also proves the walk descends."""
    bad = copy.deepcopy(rendered_config)
    bad["webauthn"]["not_a_real_authelia_key"] = True
    assert unknown_config_keys(bad, validator), (
        "an unknown key under webauthn must be reported; if it is not, this guard passes on "
        "the config that takes SSO down"
    )


def test_the_smtp_key_that_caused_1464_is_flagged(rendered_config, validator):
    """The literal key from #1464, two levels down under `notifier.smtp`."""
    bad = copy.deepcopy(rendered_config)
    bad["notifier"]["smtp"]["disable_startup_check"] = True
    paths = [path for path, _msg in unknown_config_keys(bad, validator)]
    assert "notifier/smtp" in paths, (
        f"the #1464 key must be reported at notifier/smtp; got {paths!r}"
    )


def test_an_unknown_top_level_key_is_flagged(rendered_config, validator):
    bad = copy.deepcopy(rendered_config)
    bad["not_a_real_authelia_section"] = {}
    assert unknown_config_keys(bad, validator)


# --- the schema is capable of saying no ---------------------------------------------


def test_the_vendored_schema_declares_the_sections_this_guard_reads(schema):
    """Non-vacuity: an emptied or restructured schema must fail here, not silently pass above."""
    missing = REQUIRED_DEFS - set(schema.get("$defs", {}))
    assert not missing, (
        f"the vendored schema is missing {sorted(missing)}. Every check above would validate "
        f"against a schema that declares nothing and report a clean config. Re-run "
        f"scripts/validate/refresh_crd_schemas.py and read the diff"
    )


@pytest.mark.parametrize("definition", sorted(REQUIRED_DEFS))
def test_each_section_refuses_keys_it_does_not_declare(schema, definition):
    """`additionalProperties: false` is the single property this whole guard rests on.

    Without it the schema accepts any key and every assertion above passes on a config that
    crashes the portal — green while checking nothing.
    """
    assert schema["$defs"][definition].get("additionalProperties") is False, (
        f"{definition} no longer sets additionalProperties: false, so unknown keys under it "
        f"are accepted and this guard cannot see the failure it exists to catch"
    )


def test_the_vendored_schema_matches_the_pinned_image():
    """A schema from another minor accepts keys the running binary rejects, or the reverse."""
    defaults = yaml_fast.safe_load(AUTHELIA_DEFAULTS.read_text())
    image = defaults["authelia_k8s_image"]
    tag = image.rsplit(":", 1)[-1]
    major, minor = tag.split(".")[:2]
    assert f"v{major}.{minor}" == SCHEMA_MINOR, (
        f"{image} is a {major}.{minor} release but the vendored schema is {SCHEMA_MINOR}. "
        f"Bump AUTHELIA_SCHEMA_MINOR in scripts/validate/refresh_crd_schemas.py, re-run it, "
        f"and update SCHEMA_MINOR here"
    )
