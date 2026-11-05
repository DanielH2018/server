"""Tests for scripts/lib/yaml_fast.py.

Run: uv run pytest scripts/lib/tests/test_yaml_fast.py
"""

import ast

import pytest
import yaml
from _row_table import Census, Subject, check, proof_problems, tracked

from lib import yaml_fast


def test_the_c_loader_is_the_one_actually_selected():
    """The whole point of the module, and the half that fails SILENTLY without this.

    `getattr(yaml, "CSafeLoader", yaml.SafeLoader)` degrades to the pure-Python parser on
    a build without libyaml, with no error and no log line — the suite stays green and
    30% slower. Asserting `Loader is yaml.CSafeLoader` is what makes that visible; an
    assertion that merely says "a loader was chosen" passes forever on the fallback.
    """
    assert yaml_fast.Loader is yaml.CSafeLoader


def test_it_parses_what_safe_load_parses():
    doc = "a: 1\nb:\n  - x\n  - y\nc: {d: true}\n"
    # Deliberately compares against PyYAML's own `safe_load`: this is the equivalence the
    # whole swap rests on, so it is the one place in the repo that must keep calling it.
    assert yaml_fast.safe_load(doc) == yaml.safe_load(doc)


def test_safe_load_all_yields_every_document():
    stream = "a: 1\n---\nb: 2\n---\nc: 3\n"
    assert list(yaml_fast.safe_load_all(stream)) == [{"a": 1}, {"b": 2}, {"c": 3}]


def test_a_python_object_tag_is_still_refused():
    """The schema is unchanged, so the thing safe_load exists to refuse is still refused.

    This is the rejecting half: a parser swap that quietly became `yaml.load` with the
    unsafe loader would pass every test above and construct arbitrary objects.
    """
    with pytest.raises(yaml.YAMLError):
        yaml_fast.safe_load("!!python/object/apply:os.system ['echo hi']\n")


def test_malformed_yaml_still_raises_yamlerror():
    """Callers catch `yaml.YAMLError`; libyaml's errors must stay inside that hierarchy."""
    with pytest.raises(yaml.YAMLError):
        yaml_fast.safe_load("a: [1, 2\nb: }\n")


@pytest.mark.parametrize(
    "text",
    [
        "a: [unclosed\n",
        "a: 2026-13-45\n",
        "a: !!bool x\n",
        "a: !!timestamp x\n",
        "{<<: " * 1200 + "{a: 1}" + "}" * 1200,
    ],
    ids=["syntax", "impossible-date", "bad-bool", "bad-timestamp", "nested-merges"],
)
def test_parse_yaml_raises_one_exception_for_a_text_that_does_not_load(text):
    with pytest.raises(yaml_fast.UnparsableYaml):
        yaml_fast.parse_yaml(text)


def test_the_parse_failure_is_caught_by_a_yaml_error_or_a_value_error_catch():
    """Callers around `render_guard` catch one or the other, and neither may stop catching."""
    assert issubclass(yaml_fast.UnparsableYaml, yaml.YAMLError)
    assert issubclass(yaml_fast.UnparsableYaml, ValueError)


def _narrowing_modules() -> list[str]:
    return [
        *tracked("scripts/deploy_tools/narrow_lib/*.py"),
        "scripts/deploy_tools/narrow_setup.py",
        "scripts/lib/narrow_git.py",
        "scripts/lib/render_guard.py",
        "scripts/lib/k8s_role_callers.py",
    ]


def _direct_parses(subject: Subject) -> list[str]:
    """Each CALL to `safe_load[_all]` on `yaml` or `yaml_fast`; a docstring is not a call."""
    return [
        f"line {node.lineno}: {node.func.value.id}.{node.func.attr}"
        for node in ast.walk(ast.parse(subject.text))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"safe_load", "safe_load_all"}
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"yaml", "yaml_fast"}
    ]


NARROWING_CENSUS = Census(
    name="the-narrowing-parses-yaml-through-parse-yaml",
    reason=(
        "PyYAML raises more than `yaml.YAMLError`: ValueError for an impossible date, KeyError "
        "for `!!bool x`. `parse_yaml` raises one exception for all of them, so a caller refuses "
        "every unloadable text as it refuses a syntax error. Fixing the sites one at a time "
        "did not converge (#7293)."
    ),
    files=_narrowing_modules,
    offence=_direct_parses,
    red=(
        Subject("a.py", "doc = yaml.safe_load(text)\n"),
        Subject("b.py", "doc = yaml_fast.safe_load(text)\n"),
        Subject("c.py", "docs = list(yaml_fast.safe_load_all(text))\n"),
    ),
    green=(
        Subject("a.py", "doc = yaml_fast.parse_yaml(text)\n"),
        Subject("b.py", '"""`yaml.safe_load` raises ValueError on a bad date."""\n'),
    ),
    must_find=frozenset(
        {
            "scripts/deploy_tools/narrow_lib/task_walk.py",
            "scripts/lib/render_guard.py",
            "scripts/lib/k8s_role_callers.py",
        }
    ),
)


def test_no_narrowing_module_parses_yaml_directly():
    problems = check(NARROWING_CENSUS)
    assert not problems, "\n".join(problems)


def test_the_narrowing_census_flags_a_direct_parse_and_passes_parse_yaml():
    assert not proof_problems(NARROWING_CENSUS)
