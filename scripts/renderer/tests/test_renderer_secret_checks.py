"""The five secret rules over small fixtures, and `PASSTHROUGH_FILTERS` (target slice R2).

Each rule has an `_is_clean` and `_is_flagged` pair here. The checks against the real tree are
in `test_renderer_secret_checks_tree.py`.

The membership test calls a filter as a template does, once with the sentinel `r2-sentinel`
for the secret key and once with `a.b-c.example`. The first output, serialised as JSON, must
hold the sentinel, and replacing the sentinel there with `a.b-c.example` must give the second
output exactly. `PASSTHROUGH_FILTERS` is empty, so a fixture filter is the test's only subject
until a repo filter joins the set.

Run: uv run pytest scripts/renderer/tests/test_renderer_secret_checks.py
"""

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from lib.repo_paths import SHARED_TPL
from renderer.renderer_lib.cluster import RenderedTag
from renderer.renderer_lib.environment import renderer_env
from renderer.renderer_lib.jinja_aliases import scan_templates
from renderer.renderer_lib.jinja_uses import Policy, Use, embed_refusals
from renderer.renderer_lib.placeholders import apply_placeholders, sentinel
from renderer.renderer_lib.secret_checks import (
    CONFINED_TO_SECRET,
    KNOWN_KEY,
    NO_BARE_DOLLAR_BRACE,
    NO_SOPS_MANIFEST_KEY,
    PASSTHROUGH_FILTERS,
    RULE5_FLOOR,
    SOPS_MANIFEST_KEYS,
    NamedUse,
    Rule5,
    VarsLookupSite,
    Violation,
    check,
    output_members,
    output_violations,
    rule5,
    rule5_members,
    scan_template,
)

SENTINEL = "r2-sentinel"
OTHER = "a.b-c.example"

# Each `PASSTHROUGH_FILTERS` member mapped to every call shape the templates write, each a
# function of the secret key. Empty while the set is: a member added without its calls here
# fails `test_every_member_has_its_template_calls`.
CALLS: dict[str, list[Callable[[str], object]]] = {}


def check_membership(call):
    """Raise AssertionError unless `call(key)` places `key` into its output unchanged."""
    with_sentinel = json.dumps(call(SENTINEL))
    assert SENTINEL in with_sentinel, (
        f"the output {with_sentinel} does not hold the sentinel {SENTINEL!r}"
    )
    replaced = with_sentinel.replace(SENTINEL, OTHER)
    expected = json.dumps(call(OTHER))
    assert replaced == expected, (
        f"replacing the sentinel gives {replaced}, but the call with {OTHER!r} gives {expected}"
    )


def test_every_member_has_its_template_calls():
    assert set(CALLS) == PASSTHROUGH_FILTERS
    for name, calls in CALLS.items():
        assert calls, f"{name} names no call shape"
        for call in calls:
            check_membership(call)


def test_membership_of_an_interpolating_filter_is_clean():
    def fixture_filter(entries, name, domain):
        return f"https://{name}.{domain}/"

    check_membership(lambda key: fixture_filter([], "headlamp", key))


@pytest.mark.parametrize(
    ("transform", "message"),
    [
        (str.upper, "does not hold the sentinel"),
        (lambda key: key.replace(".", "-"), "replacing the sentinel gives"),
    ],
)
def test_membership_of_a_transforming_filter_is_flagged(transform, message):
    def fixture_filter(entries, name, domain):
        return f"https://{name}.{transform(domain)}/"

    with pytest.raises(AssertionError, match=message):
        check_membership(lambda key: fixture_filter([], "headlamp", key))


# -- the five rules, each over a small fixture --------------------------------------------

KEY, SOPS_KEY = "fixture_key", "pi_peer_backup_ssh_key"
FIXTURE_KEYS = [KEY, SOPS_KEY, "email"]
SECRET_DOC = (
    "apiVersion: v1\nkind: Secret\nmetadata:\n  name: s\nstringData:\n  v: {}\n"
)
CONFIGMAP_DOC = (
    "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\ndata:\n  v: {}\n"
)


def _render(text: str):
    return apply_placeholders([RenderedTag("t", {"f.yaml": text})], FIXTURE_KEYS)


def _rules_1_to_4(text: str) -> list[tuple[str, str, str]]:
    """`(rule, site, key)` of each rule 1 to 4 breach in a one-file fixture render."""
    return [
        (v.rule, v.site, v.key) for v in output_violations(_render(text), FIXTURE_KEYS)
    ]


def test_known_key_is_clean():
    assert _rules_1_to_4(SECRET_DOC.format(sentinel(KEY))) == []


def test_known_key_is_flagged():
    render = _render(SECRET_DOC.format(sentinel(KEY)))
    # Added after the passes, so no escape reaches it.
    render.tags[0].files["f.yaml"] += "  w: ${not_a_key}\n"
    found = [(v.rule, v.key) for v in output_violations(render, FIXTURE_KEYS)]
    assert (KNOWN_KEY, "not_a_key") in found


def test_no_bare_dollar_brace_is_clean():
    # Pass 2 escapes the shell `${HOME}` in a directory that substitutes.
    assert _rules_1_to_4(SECRET_DOC.format(sentinel(KEY)) + "  w: ${HOME}\n") == []


def test_no_bare_dollar_brace_is_flagged():
    # A shell `${HOME}` that missed the escape, a known key whose sentinel never reached this
    # directory, and a sentinel fragment a transform left behind.
    render = _render(SECRET_DOC.format(sentinel(KEY)))
    render.tags[0].files["f.yaml"] += (
        "  u: ${HOME}\n  w: ${email}\n  x: homelab_secret__fixture_key__end\n"
    )
    found = {(v.rule, v.key) for v in output_violations(render, FIXTURE_KEYS)}
    assert found == {
        (KNOWN_KEY, "HOME"),
        (NO_BARE_DOLLAR_BRACE, "HOME"),
        (NO_BARE_DOLLAR_BRACE, "email"),
        (NO_BARE_DOLLAR_BRACE, "sentinel"),
    }


def test_confined_to_secret_is_clean():
    text = (
        CONFIGMAP_DOC.format(sentinel("email"))
        + "---\n"
        + SECRET_DOC.format(sentinel(KEY))
    )
    assert _rules_1_to_4(text) == []


def test_confined_to_secret_is_flagged():
    assert _rules_1_to_4(CONFIGMAP_DOC.format(sentinel(KEY))) == [
        (CONFINED_TO_SECRET, "ConfigMap/c", KEY)
    ]


def test_no_sops_manifest_key_is_clean():
    assert _rules_1_to_4(SECRET_DOC.format(f'"{sentinel(KEY)}"')) == []


def test_no_sops_manifest_key_is_flagged():
    assert _rules_1_to_4(SECRET_DOC.format(sentinel(SOPS_KEY))) == [
        (NO_SOPS_MANIFEST_KEY, "Secret/s", SOPS_KEY)
    ]


def _refused(source: str, **kwargs) -> list[tuple[str, str]]:
    """`(key, form)` of each use rule 5 refuses in a fixture template."""
    scan = scan_template(source, frozenset(FIXTURE_KEYS), **kwargs)
    return [(use.key, use.forms[-1]) for use in scan.uses if use.refused]


@pytest.mark.parametrize(
    "source",
    [
        "{{ fixture_key }}",
        "{{ fixture_key | default('') }}",
        "{{ ('x' ~ fixture_key) | to_json }}",
        "{% if fixture_key is defined %}x{% endif %}",
        "{% if fixture_key is not defined %}x{% endif %}",
        "{{ fixture_key | indent(4) }}",
        "{{ pi_peer_backup_ssh_key | to_json }}",
        "{% for h in [('a.' ~ email, 1)] + ([('b.' ~ email, 2)] if x else []) %}"
        "{% endfor %}",
        "{% filter indent(4) %}{{ fixture_key }}{% endfilter %}",
        "{% set x %}{{ fixture_key }}{% endset %}",
        # #4939's shapes, each used bare.
        "{% set x %}{{ fixture_key }}{% endset %}{{ x }}",
        "{% macro m() %}{{ fixture_key }}{% endmacro %}{{ m() }}",
        "{% macro m() %}{{ caller() }}{% endmacro %}"
        "{% call m() %}{{ fixture_key }}{% endcall %}",
    ],
)
def test_no_secret_expression_is_clean(source):
    assert _refused(source) == []


@pytest.mark.parametrize(
    ("source", "form"),
    [
        ("{{ (fixture_key | string).split(':', 1) | last }}", "filter string"),
        ("{{ fixture_key.split(':') }}", "attribute"),
        ("{{ fixture_key[1:] }}", "subscript"),
        ("{{ fixture_key | trim }}", "filter trim"),
        ("{{ fixture_key | upper }}", "filter upper"),
        ("{{ fixture_key | b64encode }}", "filter b64encode"),
        ("{% if fixture_key %}x{% endif %}", "truthiness test in an if"),
        ("{% if fixture_key | default('') %}x{% endif %}", "truthiness test in an if"),
        ("{% if fixture_key | length %}x{% endif %}", "filter length"),
        ("{% if fixture_key == 'x' %}x{% endif %}", "comparison"),
        ("{{ pi_peer_backup_ssh_key | indent(4) }}", "filter indent"),
        # A block filter applies to the text its body writes.
        ("{% filter trim %}{{ fixture_key }}{% endfilter %}", "block filter trim"),
        ("{% filter lower %}{{ fixture_key }}{% endfilter %}", "block filter lower"),
        ("{% set x | trim %}{{ fixture_key }}{% endset %}", "block filter trim"),
        (
            "{% filter indent(4) %}{{ pi_peer_backup_ssh_key }}{% endfilter %}",
            "block filter indent",
        ),
        # #4938: a block filter around a macro argument, a binding or a loop.
        (
            "{% macro m(v) %}{{ v }}{% endmacro %}"
            "{% filter trim %}{{ m(fixture_key) }}{% endfilter %}",
            "block filter trim",
        ),
        (
            "{% filter trim %}{% set x = fixture_key %}{{ x }}{% endfilter %}",
            "block filter trim",
        ),
        (
            "{% filter trim %}{% for c in fixture_key %}{{ c }}{% endfor %}{% endfilter %}",
            "block filter trim",
        ),
        # #4939: a filter where a captured block, a macro or a call body is used.
        ("{% set x %}{{ fixture_key }}{% endset %}{{ x | trim }}", "filter trim"),
        (
            "{% macro m() %}{{ fixture_key }}{% endmacro %}{{ m() | trim }}",
            "filter trim",
        ),
        (
            "{% macro m() %}{{ caller() | trim }}{% endmacro %}"
            "{% call m() %}{{ fixture_key }}{% endcall %}",
            "filter trim",
        ),
        (
            "{% macro m() %}{{ fixture_key }}{% endmacro %}"
            "{% filter trim %}{% call m() %}{% endcall %}{% endfilter %}",
            "block filter trim",
        ),
    ],
)
def test_no_secret_expression_is_flagged(source, form):
    (refused,) = _refused(source)
    assert refused[1] == form


IMPORTER = "{% from 'b.j2' import m %}{{ m()"


def _imported_refusals(importer: str) -> list[tuple[str, str]]:
    """`(key, form)` refused in `importer`, which imports `m` from a template outputting a key."""
    sources = {
        Path("a.j2"): importer,
        Path("b.j2"): "{% macro m() %}{{ fixture_key }}{% endmacro %}",
    }
    policy = Policy(PASSTHROUGH_FILTERS, SOPS_MANIFEST_KEYS)
    env = renderer_env(SHARED_TPL)
    scans = scan_templates(
        sources, frozenset(FIXTURE_KEYS), policy, env, resolve=lambda _, n: Path(n)
    )
    return [(use.key, use.forms[-1]) for use in scans[Path("a.j2")].uses if use.refused]


def test_a_macro_imported_with_from_used_bare_is_clean():
    assert _imported_refusals(IMPORTER + " }}") == []


def test_a_macro_imported_with_from_transformed_is_flagged():
    assert _imported_refusals(IMPORTER + " | trim }}") == [(KEY, "filter trim")]


def _embed_refusals(includer: str, embedded: str) -> list[tuple[str, str]]:
    """`(key, form)` refused when `includer` embeds a template whose source is `embedded`."""
    names = frozenset(FIXTURE_KEYS)
    scans = {
        Path("a.j2"): scan_template(includer, names),
        Path("b.j2"): scan_template(embedded, names),
    }
    found = embed_refusals(scans, lambda path, embed: Path("b.j2"), SOPS_MANIFEST_KEYS)
    return [(key, form) for _, key, form in found]


INCLUDE_TRIM = "{% filter trim %}{% include 'b.j2' %}{% endfilter %}"
LOOKUP = "{{ lookup('template', playbook_dir + '/b.j2') | %s }}"


@pytest.mark.parametrize(
    ("includer", "embedded"),
    [
        (LOOKUP % "indent(4)", "{{ fixture_key }}"),
        (INCLUDE_TRIM, "no key here"),
        ("{% filter indent(4) %}{% include 'b.j2' %}{% endfilter %}", "{{ email }}"),
    ],
)
def test_an_embed_that_keeps_its_keys_intact_is_clean(includer, embedded):
    assert _embed_refusals(includer, embedded) == []


@pytest.mark.parametrize(
    ("includer", "embedded", "refused"),
    [
        (INCLUDE_TRIM, "{{ fixture_key }}", (KEY, "embeds through block filter trim")),
        (LOOKUP % "trim", "{{ fixture_key }}", (KEY, "embeds through filter trim")),
        (
            LOOKUP % "indent(4)",
            "{{ pi_peer_backup_ssh_key | to_json }}",
            (SOPS_KEY, "embeds through indent"),
        ),
    ],
)
def test_an_embed_that_transforms_its_keys_is_flagged(includer, embedded, refused):
    assert _embed_refusals(includer, embedded) == [refused]


def test_named_members_and_the_floor_found_are_clean():
    member = NamedUse("t.j2", KEY, "filter default", 1)
    use = Use(KEY, ("filter default", "output"), False)
    result = Rule5(naming=["t.j2"] * RULE5_FLOOR, accepted=[("t.j2", use)])
    assert (
        rule5_members(result, FIXTURE_KEYS, frozenset(), (member,), not_aliases=())
        == []
    )


def test_a_rule5_result_under_the_floor_is_flagged():
    result = Rule5(naming=["t.j2"] * (RULE5_FLOOR - 1))
    (problem,) = rule5_members(result, FIXTURE_KEYS, frozenset(), (), not_aliases=())
    assert f"{RULE5_FLOOR - 1} templates name a secret key, under the floor" in problem


def test_a_rule5_result_missing_a_named_use_is_flagged():
    member = NamedUse("t.j2", KEY, "filter default", 1)
    use = Use(KEY, ("output",), False)
    result = Rule5(naming=["t.j2"] * RULE5_FLOOR, accepted=[("t.j2", use)])
    (problem,) = rule5_members(
        result, FIXTURE_KEYS, frozenset(), (member,), not_aliases=()
    )
    assert (
        f"named member gone: 1 accepted uses of {KEY} through filter default" in problem
    )


def test_every_sops_manifest_key_in_the_secrets_file_is_clean():
    render = _render(SECRET_DOC.format(sentinel(KEY)))
    assert output_members(render, [*FIXTURE_KEYS, *SOPS_MANIFEST_KEYS]) == []


def test_a_sops_manifest_key_absent_from_the_secrets_file_is_flagged():
    render = _render(SECRET_DOC.format(sentinel(KEY)))
    absent = sorted(SOPS_MANIFEST_KEYS - set(FIXTURE_KEYS))
    assert output_members(render, FIXTURE_KEYS) == [
        f"no-sops-manifest-key: {k} is in SOPS_MANIFEST_KEYS and not in "
        "ansible/vars/secrets.yml"
        for k in absent
    ]


def test_a_key_passed_to_a_filter_outside_the_set_is_flagged():
    source = "{{ entries | fixture_href('x', email) }}"
    assert _refused(source) == [("email", "argument to fixture_href")]


def test_a_key_passed_to_a_filter_in_a_fixture_set_is_clean():
    source = "{{ entries | fixture_href('x', email) }}"
    passthrough = PASSTHROUGH_FILTERS | {"fixture_href"}
    assert _refused(source, passthrough=passthrough) == []


def test_a_non_literal_vars_lookup_used_bare_is_clean():
    assert _refused("{{ lookup('vars', check.token) }}") == []


def test_a_non_literal_vars_lookup_transformed_is_flagged():
    assert _refused("{{ lookup('vars', check.token) | trim }}") == [
        ("lookup('vars', check.token)", "filter trim")
    ]


def test_a_literal_vars_lookup_transformed_is_flagged_as_its_key():
    assert _refused("{{ lookup('vars', 'fixture_key') | trim }}") == [
        (KEY, "filter trim")
    ]


def test_an_allowlisted_vars_lookup_is_clean():
    source = "{% if lookup('vars', job.replicas_var) | int > 0 %}x{% endif %}"
    assert _refused(source, non_secret=frozenset({"job.replicas_var"})) == []
    assert _refused(source) == [("lookup('vars', job.replicas_var)", "filter int")]


def _site(names: set[str]) -> VarsLookupSite:
    return VarsLookupSite("t.yaml.j2", "job.replicas_var", lambda: names)


def _rule5_over(tmp_path, source: str) -> Rule5:
    template = tmp_path / "t.yaml.j2"
    template.write_text(source)
    return rule5(FIXTURE_KEYS, [template], frozenset({_site(set())}), root=tmp_path)


LOOKUP = "{{ lookup('vars', job.replicas_var) | int }}"


def test_a_non_secret_vars_lookup_entry_is_clean(tmp_path):
    result = _rule5_over(tmp_path, LOOKUP)
    site = _site({"terraria_k8s_replicas"})
    assert result.violations == []
    assert rule5_members(result, FIXTURE_KEYS, frozenset({site}), (), 0, ()) == []


def test_a_non_secret_vars_lookup_entry_naming_a_key_is_flagged(tmp_path):
    result = _rule5_over(tmp_path, LOOKUP)
    sites = frozenset({_site({KEY})})
    (problem,) = rule5_members(result, FIXTURE_KEYS, sites, (), 0, ())
    assert f"can read the secret keys ['{KEY}']" in problem


def test_a_non_secret_vars_lookup_entry_whose_site_has_gone_is_flagged(tmp_path):
    result = _rule5_over(tmp_path, "{{ x }}")
    sites = frozenset({_site(set())})
    (problem,) = rule5_members(result, FIXTURE_KEYS, sites, (), 0, ())
    assert "stale NON_SECRET_VARS_LOOKUPS entry" in problem


# -- the ratchet ----------------------------------------------------------------------------

ENTRY = Violation(CONFINED_TO_SECRET, "t", "f.yaml", "ConfigMap/c", KEY)


def _check_over(text: str, known: frozenset[Violation]):
    # A stand-in rule 5 result, so the ratchet is read over rules 1 to 4 alone.
    rule5_result = Rule5(naming=["t.yaml.j2"] * RULE5_FLOOR)
    return check(_render(text), FIXTURE_KEYS, known, rule5_result)


def test_a_known_violation_is_clean():
    report = _check_over(CONFIGMAP_DOC.format(sentinel(KEY)), frozenset({ENTRY}))
    assert (report.unknown, report.stale) == ([], [])


def test_a_known_violation_that_no_longer_matches_is_flagged():
    report = _check_over(SECRET_DOC.format(sentinel(KEY)), frozenset({ENTRY}))
    assert report.stale == [ENTRY]
    assert not report.ok()


def test_a_known_violation_of_a_tag_not_rendered_is_not_stale():
    entry = ENTRY._replace(tag="other")
    report = _check_over(SECRET_DOC.format(sentinel(KEY)), frozenset({entry}))
    assert report.stale == []
