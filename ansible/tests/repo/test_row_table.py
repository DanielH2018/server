"""The census harness itself goes red: subject gone, floor, named member, stale allow, offender.

`ansible/tests/k8s/test_rendered_properties.py` proves the same verdict through the rendered
table; these prove it through `Census`, whose subjects come from the tracked tree.

Run: uv run pytest ansible/tests/repo/test_row_table.py
"""

import ast
from dataclasses import replace

import pytest
from _row_table import (
    REPO,
    Census,
    Subject,
    check,
    Sharing,
    proof_problems,
    tracked,
)


def _toy(**overrides) -> Census:
    """A row whose files must not contain the word `bad`."""
    toy = Census(
        name="toy",
        reason="toy",
        files=lambda: [],
        offence=lambda s: ["says bad"] if "bad" in s.text else [],
        red=(Subject("r", "bad"),),
        green=(Subject("g", "good"),),
    )
    return replace(toy, **overrides)


def test_check_is_clean_when_every_file_holds():
    files = [Subject("a", "good"), Subject("b", "fine")]
    assert check(_toy(must_find=frozenset({"a"})), files) == []


def test_check_is_flagged_when_a_file_offends():
    problems = check(_toy(), [Subject("a", "good"), Subject("b", "bad")])
    assert "toy: b: says bad" in problems, problems


def test_check_is_flagged_when_the_subject_is_gone():
    assert check(_toy(), []) == [
        "toy: subject gone: delete this row (the selector matched nothing)"
    ]


def test_check_is_flagged_below_the_floor_or_missing_a_named_member():
    problems = check(
        _toy(min_matches=2, must_find=frozenset({"z"})), [Subject("a", "")]
    )
    assert any("floor is 2" in p for p in problems), problems
    assert any("never matched ['z']" in p for p in problems), problems


def test_check_is_clean_on_an_allowed_offender_and_flags_a_stale_allow_entry():
    allowed = _toy(allow={"b": "known exception"})
    assert check(allowed, [Subject("a", "good"), Subject("b", "bad")]) == []
    problems = check(allowed, [Subject("a", "good"), Subject("b", "good")])
    assert any("allow entry 'b' is stale" in p for p in problems), problems


def test_a_counted_row_floors_matches_and_finds_only_files_holding_one():
    counted = _toy(
        count=lambda s: s.text.count("ship"),
        min_matches=3,
        must_find=frozenset({"a", "b"}),
    )
    assert check(counted, [Subject("a", "ship ship"), Subject("b", "ship")]) == []
    problems = check(counted, [Subject("a", "ship ship"), Subject("b", "none")])
    assert any("2 matches, floor is 3" in p for p in problems), problems
    assert any("never matched ['b']" in p for p in problems), problems


def test_a_row_whose_red_subject_passes_is_flagged():
    assert proof_problems(_toy(red=(Subject("r", "good"),))) == ["red 'r' passed"]
    assert proof_problems(_toy()) == []


def test_tracked_reads_this_checkout():
    """Without this, a `tracked` that returned nothing would turn every row into `subject gone`
    rather than into a pass, which is loud, but the failure would name the wrong cause."""
    assert "ansible/tests/_row_table.py" in tracked("ansible/tests/*.py")


@pytest.fixture
def parses(monkeypatch):
    """Each `ast.parse` source, counted at the standard library's own entry point."""
    seen = []
    real = ast.parse

    def counting(source, *args, **kwargs):
        seen.append(source)
        return real(source, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", counting)
    return seen


def _names_row(sharing: Sharing, name: str, census=()) -> Census:
    """A shared-row candidate whose offence reads the subject's tree."""
    return _toy(
        name=name,
        files=lambda: list(census),
        offence=lambda s: [
            f"{name} {n.id}"
            for n in ast.walk(sharing.parse(s.text))
            if isinstance(n, ast.Name)
        ],
    )


def test_shared_rows_find_their_hits_from_one_parse_and_keep_no_tree(parses):
    sharing = Sharing()
    first, second = sharing.share(
        (_names_row(sharing, "one"), _names_row(sharing, "two"))
    )
    subject = Subject("m.py", "x = y\n")
    assert first.offence(subject) == ["one x", "one y"]
    assert second.offence(subject) == ["two x", "two y"]
    assert parses == ["x = y\n"]
    assert sharing.held == {}


def test_a_shared_row_that_raises_fails_only_its_own_row(parses):
    sharing = Sharing()
    textual, parsing = sharing.share(
        (
            _toy(name="textual", offence=lambda s: ["hit"] if "for" in s.text else []),
            _names_row(sharing, "parsing"),
        )
    )
    subject = Subject("m.py", "for x in y:\n")
    assert textual.offence(subject) == ["hit"]
    with pytest.raises(SyntaxError):
        parsing.offence(subject)


def test_a_census_module_read_ahead_is_parsed_once(parses):
    sharing = Sharing()
    (row,) = sharing.share((_names_row(sharing, "one", census=["m.py"]),))
    sharing.parse_ahead(REPO / "m.py", "x = 1\n")
    sharing.parse_ahead(REPO / "elsewhere.py", "z = 1\n")
    assert row.offence(Subject("m.py", "x = 1\n")) == ["one x"]
    assert parses == ["x = 1\n", "z = 1\n"]
    assert sharing.held == {}, "a module outside the census was held after its parse"
