"""A reader returns a value only when every copy of it in the source agrees."""

import pytest

from docs.doc_values_lib.readers import match, phrase
from docs.doc_values_lib.rows_roles import _rungs


def _source(tmp_path, text):
    path = tmp_path / "source.sh"
    path.write_text(text)
    return path


def test_copies_that_agree_read_as_one_value(tmp_path):
    path = _source(tmp_path, "ssh -o ConnectTimeout=10\nssh -o ConnectTimeout=10\n")
    assert match(r"ConnectTimeout=(\d+)")(path) == "10"


def test_copies_that_disagree_are_flagged(tmp_path):
    path = _source(tmp_path, "ssh -o ConnectTimeout=10\nssh -o ConnectTimeout=30\n")
    with pytest.raises(ValueError, match=r"expected one value, found \['10', '30'\]"):
        match(r"ConnectTimeout=(\d+)")(path)


def test_a_value_with_no_phrase_is_flagged_not_guessed():
    assert phrase({"60": "an hour"})("60") == "an hour"
    with pytest.raises(ValueError, match="no phrase for '30'"):
        phrase({"60": "an hour"})("30")


_RUNGS = """\
    movie-expiration:
      5: 15d
      10: 30d
      25: 180d
    season-expiration:
      5: 15d
      10: 20d
      {last}: 240d
"""


def test_rungs_read_as_the_list_every_expiration_map_shares(tmp_path):
    assert _rungs(_source(tmp_path, _RUNGS.format(last=25))) == "5, 10 and 25"


def test_expiration_maps_that_disagree_on_their_rungs_are_flagged(tmp_path):
    with pytest.raises(ValueError, match="disagree on their rungs"):
        _rungs(_source(tmp_path, _RUNGS.format(last=20)))
