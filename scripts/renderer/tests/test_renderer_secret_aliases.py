"""Rule 5's two alias clauses over small fixtures (target slice R2, PR 2b).

The template-local clause binds a name by `{% set %}`, a set block, `{% with %}`, a `for`
target or a macro parameter, and holds each use of it to the key's forms. The second clause
reads role defaults and inventory. The checks against the real tree are in
`test_renderer_secret_checks_tree.py`.

Run: uv run pytest scripts/renderer/tests/test_renderer_secret_aliases.py
"""

import pytest

from renderer.renderer_lib.secret_checks import (
    NO_SECRET_EXPRESSION,
    PASSTHROUGH_FILTERS,
    rule5,
    scan_template,
)
from renderer.renderer_lib.var_aliases import not_alias_problems

KEY = "fixture_key"
KEYS = frozenset({KEY, "email"})
# A fixture member of `PASSTHROUGH_FILTERS`, whose output is a container alias.
PASSTHROUGH = PASSTHROUGH_FILTERS | {"fixture_rules"}
RULES = "{% for rule in entries | fixture_rules(fixture_key, []) %}"


def _refused(source: str, **kwargs) -> list[tuple[str, str]]:
    """`(key, form)` of each use rule 5 refuses in a fixture template."""
    scan = scan_template(source, KEYS, passthrough=PASSTHROUGH, **kwargs)
    return [(use.key, use.forms[-1]) for use in scan.uses if use.refused]


# Each binding form, with `{}` where the bound name `x` is used.
BINDINGS = [
    "{% set x = 'a.' ~ fixture_key %}{}",
    "{% for x in ['a.' ~ fixture_key] %}{}{% endfor %}",
    "{% set x %}a.{{ fixture_key }}{% endset %}{}",
    "{% with x = 'a.' ~ fixture_key %}{}{% endwith %}",
    "{% for x, b in [('a.' ~ fixture_key, 1)] %}{}{% endfor %}",
    "{% macro m(x) %}{}{% endmacro %}{{ m('a.' ~ fixture_key) }}",
]


@pytest.mark.parametrize("binding", BINDINGS)
def test_an_alias_used_bare_is_clean(binding):
    assert _refused(binding.replace("{}", "{{ x }}")) == []


@pytest.mark.parametrize("binding", BINDINGS)
def test_an_alias_transformed_is_flagged(binding):
    assert _refused(binding.replace("{}", "{{ x | trim }}")) == [(KEY, "filter trim")]


@pytest.mark.parametrize(
    "source",
    [
        # A container alias may be tested in an `{% if %}`, iterated, and have a part selected.
        "{% set t = [{'v': 'a.' ~ fixture_key}] %}{% if t %}{{ t | to_json }}{% endif %}",
        RULES
        + "{% for h in rule.domain %}{{ h }}{% endfor %}{{ rule.policy }}{% endfor %}",
        # Only the other target of an unpacked loop holds the key, and a loop target is
        # gone after its loop.
        "{% for a, b in [('a.' ~ fixture_key, 'c')] %}{{ b | trim }}{% endfor %}",
        "{% for x in [fixture_key] %}{% endfor %}{{ x | trim }}",
        # A rebinding to a value with no key ends the alias.
        "{% set x = fixture_key %}{% set x = 'a' %}{{ x | trim }}",
    ],
)
def test_an_alias_shape_used_as_it_allows_is_clean(source):
    assert _refused(source) == []


@pytest.mark.parametrize(
    ("source", "form"),
    [
        (
            "{% set x = 'a.' ~ fixture_key %}{% if x %}x{% endif %}",
            "truthiness test in an if",
        ),
        ("{% set x = 'a.' ~ fixture_key %}{{ x.split('.') }}", "attribute"),
        (RULES + "{{ rule.policy | trim }}{% endfor %}", "filter trim"),
        # Either branch may bind the alias, so a use after the `if` reads both.
        (
            "{% if c %}{% set x = fixture_key %}{% else %}{% set x = 'a' %}{% endif %}"
            "{{ x | trim }}",
            "filter trim",
        ),
    ],
)
def test_an_alias_shape_used_as_it_does_not_allow_is_flagged(source, form):
    assert _refused(source) == [(KEY, form)]


def test_a_refused_binding_is_one_violation_whose_uses_add_none():
    source = "{% set x = fixture_key | trim %}{{ x | upper }}"
    assert _refused(source) == [(KEY, "filter trim")]


LOOKUP_BLOCK = "{% set c %}{{ lookup('vars', job.replicas_var) | int }}%s{% endset %}"
ALLOWED = frozenset({"job.replicas_var"})


def test_a_set_block_holding_only_an_allowlisted_lookup_is_clean():
    source = LOOKUP_BLOCK.replace("%s", "") + "{{ c | hash('sha1') }}"
    assert _refused(source, non_secret=ALLOWED) == []


def test_a_set_block_holding_an_allowlisted_lookup_and_a_key_is_flagged():
    source = LOOKUP_BLOCK.replace("%s", "{{ fixture_key }}") + "{{ c | hash('sha1') }}"
    assert _refused(source, non_secret=ALLOWED) == [(KEY, "filter hash")]


# -- role defaults and inventory -----------------------------------------------------------


def _rule5_with_defaults(tmp_path, defaults: str, template: str):
    """`(file, site, key)` of each rule 5 violation over one fixture role."""
    role = tmp_path / "roles" / "fixture"
    (role / "defaults").mkdir(parents=True)
    (role / "templates").mkdir()
    (role / "defaults" / "main.yml").write_text(defaults)
    (role / "templates" / "t.yaml.j2").write_text(template)
    result = rule5(
        KEYS,
        [role / "templates" / "t.yaml.j2"],
        frozenset(),
        root=tmp_path,
        var_files=[role / "defaults" / "main.yml"],
    )
    assert {v.rule for v in result.violations} <= {NO_SECRET_EXPRESSION}
    return [(v.file.split("/")[-2], v.site, v.key) for v in result.violations]


def test_a_role_default_aliasing_a_key_used_bare_is_clean(tmp_path):
    defaults = 'x: "https://a.{{ fixture_key }}"\ny: "{{ x }}"\n'
    assert _rule5_with_defaults(tmp_path, defaults, "{{ x }} {{ y }}") == []


@pytest.mark.parametrize(
    ("defaults", "template", "found"),
    [
        ('x: "{{ fixture_key | trim }}"\n', "{{ x }}", ("defaults", "x: filter trim")),
        ('x: "{{ fixture_key }}"\n', "{{ x | trim }}", ("templates", "filter trim")),
        # An alias of an alias counts the same way.
        (
            'x: "{{ fixture_key }}"\ny: "a.{{ x }}"\n',
            "{{ y | trim }}",
            ("templates", "filter trim"),
        ),
    ],
)
def test_a_role_default_aliasing_a_key_transformed_is_flagged(
    tmp_path, defaults, template, found
):
    assert _rule5_with_defaults(tmp_path, defaults, template) == [(*found, KEY)]


NOT_ALIAS = (("t.j2", "block"),)


def test_a_named_binding_that_is_no_alias_is_clean():
    assert not_alias_problems({("t.j2", "block", False)}, NOT_ALIAS) == []


@pytest.mark.parametrize(
    ("bindings", "message"),
    [
        ({("t.j2", "block", True)}, "block in t.j2 carries a secret key"),
        (set(), "named member gone: no binding of block in t.j2"),
    ],
)
def test_a_named_binding_that_is_an_alias_or_gone_is_flagged(bindings, message):
    (problem,) = not_alias_problems(bindings, NOT_ALIAS)
    assert message in problem
