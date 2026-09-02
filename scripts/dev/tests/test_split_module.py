"""Tests for the module splitter: the reference graph, the cut, and the spec errors it refuses.

Both functions under test take source text and return text, so no fixture files are
needed. Each rule has an accepting and a rejecting half: the cut is shown to carry the
decorators and the comment block above a definition, and shown to leave a plain
neighbour alone.

Run: uv run pytest scripts/dev/tests/test_split_module.py
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from split_module import main, references, split

SOURCE = textwrap.dedent(
    '''\
    """A module with a helper, a constant and two tests."""

    import pytest

    LIMIT = 3


    def _helper(x):
        return x + LIMIT


    # The comment block above a definition travels with it.
    # Two lines of it.
    @pytest.mark.parametrize("n", [1, 2])
    def test_moved(n):
        assert _helper(n) > n


    def test_stays():
        assert "LIMIT" and _helper(0) == 3
    '''
)

HEADER = '"""Moved tests."""\n\nimport pytest\nfrom src import _helper\n'


def test_references_list_the_top_level_names_a_body_uses():
    graph = {name: used for _, name, used in references(SOURCE)}
    assert graph["_helper"] == ["LIMIT"]
    assert graph["test_moved"] == ["_helper"]
    assert graph["LIMIT"] == []


def test_references_ignore_a_name_that_only_appears_in_a_string():
    graph = {name: used for _, name, used in references(SOURCE)}
    # "LIMIT" is a string literal in test_stays, not a Name node.
    assert graph["test_stays"] == ["_helper"]


def test_references_include_annotated_assignments():
    graph = {name: used for _, name, used in references("X: int = 1\nY = X + 1\n")}
    assert graph == {"X": [], "Y": ["X"]}


def test_split_moves_the_definition_with_its_decorators_and_comment_block():
    kept, outputs = split(
        SOURCE, {"new.py": {"header": HEADER, "names": ["test_moved"]}}
    )
    moved = outputs["new.py"]
    assert moved.startswith(HEADER.rstrip("\n") + "\n\n\n# The comment block above")
    assert '@pytest.mark.parametrize("n", [1, 2])\ndef test_moved(n):' in moved
    assert "test_moved" not in kept
    assert "# The comment block above" not in kept


def test_split_leaves_an_unnamed_neighbour_in_place():
    kept, outputs = split(
        SOURCE, {"new.py": {"header": HEADER, "names": ["test_moved"]}}
    )
    assert "def test_stays():" in kept
    assert "def _helper(x):" in kept
    assert "LIMIT = 3" in kept
    assert "test_stays" not in outputs["new.py"]


def test_split_keeps_source_order_within_a_target():
    _, outputs = split(
        SOURCE, {"new.py": {"header": HEADER, "names": ["test_moved", "_helper"]}}
    )
    moved = outputs["new.py"]
    assert moved.index("def _helper") < moved.index("def test_moved")


def test_split_refuses_a_name_claimed_twice():
    spec = {
        "a.py": {"header": "", "names": ["_helper"]},
        "b.py": {"header": "", "names": ["_helper"]},
    }
    with pytest.raises(ValueError, match="named twice"):
        split(SOURCE, spec)


def test_split_accepts_a_well_shaped_spec():
    """The clean half of the shape check: a str header and a list of str names."""
    kept, outputs = split(SOURCE, {"a.py": {"header": "", "names": ["LIMIT"]}})
    assert "LIMIT" in outputs["a.py"]
    assert "LIMIT = " not in kept


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        ({"header": 123, "names": ["LIMIT"]}, "header is int, not a string"),
        ({"header": None, "names": ["LIMIT"]}, "header is NoneType, not a string"),
        ({"names": ["LIMIT"]}, "header is NoneType, not a string"),
        ({"header": "", "names": "LIMIT"}, "names is not a list of strings"),
        ({"header": "", "names": [1]}, "names is not a list of strings"),
        ({"header": "", "names": None}, "names is not a list of strings"),
    ],
)
def test_split_refuses_a_spec_entry_of_the_wrong_shape(entry, match):
    """The spec comes from `json.loads`, so the TypedDict enforces nothing at that boundary.

    Every one of these reached the body before: a non-string header was written into the new
    file's first line, and a non-list `names` raised straight out of the `for` loop as a
    TypeError. `main()` catches ValueError only, so an unchecked shape bug printed a traceback
    where every other spec bug prints `error: ...`.
    """
    with pytest.raises(ValueError, match=match):
        split(SOURCE, {"a.py": entry})


def test_split_refuses_a_name_the_source_lacks():
    with pytest.raises(ValueError, match=r"not found in the source: \['test_absent'\]"):
        split(SOURCE, {"a.py": {"header": "", "names": ["test_absent"]}})


def test_split_refuses_one_statement_bound_for_two_targets():
    source = "A, B = 1, 2\n"
    spec = {
        "a.py": {"header": "", "names": ["A"]},
        "b.py": {"header": "", "names": ["B"]},
    }
    with pytest.raises(ValueError, match="one statement maps to two targets"):
        split(source, spec)


def test_split_does_not_move_a_name_that_only_matches_inside_a_string():
    # `LIMIT` appears as a string in test_stays; only the real assignment moves.
    kept, outputs = split(SOURCE, {"new.py": {"header": "", "names": ["LIMIT"]}})
    assert outputs["new.py"].strip().endswith("LIMIT = 3")
    assert 'assert "LIMIT"' in kept


def test_cli_split_writes_the_targets_and_rewrites_the_source(tmp_path: Path, capsys):
    src = tmp_path / "src.py"
    src.write_text(SOURCE)
    target = tmp_path / "moved.py"
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps({str(target): {"header": HEADER, "names": ["test_moved"]}})
    )

    assert main(["split", str(src), str(spec)]) == 0

    assert "def test_moved" in target.read_text()
    assert "def test_moved" not in src.read_text()
    out = capsys.readouterr().out
    assert f"{target}: 1 definitions" in out


def test_cli_split_exits_nonzero_and_writes_nothing_on_a_bad_spec(
    tmp_path: Path, capsys
):
    src = tmp_path / "src.py"
    src.write_text(SOURCE)
    target = tmp_path / "moved.py"
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({str(target): {"header": "", "names": ["test_absent"]}}))

    assert main(["split", str(src), str(spec)]) == 1

    assert not target.exists()
    assert src.read_text() == SOURCE
    assert "not found in the source" in capsys.readouterr().err


def test_cli_graph_prints_one_line_per_definition(tmp_path: Path, capsys):
    src = tmp_path / "src.py"
    src.write_text(SOURCE)
    assert main(["graph", str(src)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert any(line.endswith("test_moved -> _helper") for line in lines)
    assert len(lines) == 4
