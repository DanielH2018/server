"""The census harness itself goes red: subject gone, floor, named member, stale allow, offender.

`ansible/tests/k8s/test_rendered_properties.py` proves the same verdict through the rendered
table; these prove it through `Census`, whose subjects come from the tracked tree.

Run: uv run pytest ansible/tests/repo/test_row_table.py
"""

from dataclasses import replace

from _row_table import Census, Subject, check, proof_problems, tracked


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


def test_a_row_whose_red_subject_passes_is_flagged():
    assert proof_problems(_toy(red=(Subject("r", "good"),))) == ["red 'r' passed"]
    assert proof_problems(_toy()) == []


def test_tracked_reads_this_checkout():
    """Without this, a `tracked` that returned nothing would turn every row into `subject gone`
    rather than into a pass, which is loud, but the failure would name the wrong cause."""
    assert "ansible/tests/_row_table.py" in tracked("ansible/tests/*.py")
