"""Tests for scripts/lib/yaml_fast.py.

Run: uv run pytest scripts/lib/tests/test_yaml_fast.py
"""

import pytest
import yaml

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
