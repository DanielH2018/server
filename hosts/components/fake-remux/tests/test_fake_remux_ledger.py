"""The ledger refuses an unlisted move, and `advance` only deletes a fake it has replaced.

The property tests run under the profile `tests/hypothesis_profiles.py` loads.
"""

import pytest
from fake_remux_lib import fake_remux_replace as shell
from fake_remux_lib import fake_remux_replace_logic as rl
from fake_remux_lib.fake_remux_ledger import TRANSITIONS, State, move, parse
from hypothesis import given
from hypothesis import strategies as st


NOW = 1_000_000
POLICY = {"max_attempts": 3, "gop_max_s": 5}

GENUINE = {"quality": "Bluray-1080p Remux", "encoder": "", "keyframes": [0.0, 1.0, 2.0]}
FAKE = {**GENUINE, "encoder": "Lavc hevc_qsv"}

records = st.fixed_dictionaries(
    {
        "episodeId": st.just(13),
        "seriesId": st.just(96),
        "fakeFileId": st.just(2213),
        "state": st.sampled_from(State),
        "attempts": st.integers(0, 4),
        "lastAction": st.sampled_from([0, NOW - 60, NOW]),
    }
)
queues = st.none() | st.fixed_dictionaries(
    {"id": st.just(555), "sizeleft": st.sampled_from([0, 1])}
)
probes = st.sampled_from([None, GENUINE, FAKE])
files = st.sampled_from([None, 2213, 9999])


@pytest.mark.parametrize("state", State)
def test_a_listed_move_changes_the_state_and_an_unlisted_one_is_refused(state):
    ledger = {"13": {"state": state, "attempts": 1}}
    for to in State:
        if to in TRANSITIONS[state]:
            assert move(ledger, 13, to, attempts=2) == {
                "13": {"state": to, "attempts": 2}
            }
        else:
            with pytest.raises(ValueError, match=f"no move {state} -> {to}"):
                move(ledger, 13, to)
    assert ledger == {"13": {"state": state, "attempts": 1}}


@pytest.mark.parametrize("record", [{"state": "graped"}, {}])
def test_a_ledger_with_an_unknown_state_is_refused_by_entry(record):
    with pytest.raises(ValueError, match="ledger entry 13"):
        parse({"12": {"state": "held"}, "13": record})


def test_a_reconcile_on_an_unknown_state_fails_before_it_reaches_sonarr(tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text('{"13": {"state": "graped"}}')
    policy = tmp_path / "policy.json"
    policy.write_text("{}")
    cfg = {
        "FAKE_REMUX_REPLACE_MODE": "live",
        "LEDGER_FILE": str(ledger),
        "FAKE_REMUX_POLICY": str(policy),
    }
    with pytest.raises(ValueError, match="ledger entry 13"):
        shell.reconcile_once(cfg, sonarr=object())


@given(records, queues, probes, files)
def test_advance_deletes_only_the_fake_and_only_after_an_authentic_probe(
    record, queue, probe, file
):
    ledger = {"13": record}
    new, actions = rl.advance(
        ledger,
        {"13": queue} if queue else {},
        {"13": file} if file else {},
        {"13": probe} if probe else {},
        POLICY,
        NOW,
    )
    deletes = [a for a in actions if a["type"] == "delete_file"]
    if deletes:
        assert [a["fileId"] for a in deletes] == [record["fakeFileId"]]
        assert probe is GENUINE
        assert record["state"] in (State.GRABBED, State.VERIFYING)
        assert new["13"]["state"] == State.IMPORTING


@given(records, st.lists(st.tuples(queues, probes, files), max_size=8))
def test_a_run_of_ticks_never_deletes_the_same_fake_twice(record, ticks):
    ledger = {"13": record}
    deleted = 0
    for n, (queue, probe, file) in enumerate(ticks):
        ledger, actions = rl.advance(
            ledger,
            {"13": queue} if queue else {},
            {"13": file} if file else {},
            {"13": probe} if probe else {},
            POLICY,
            NOW + n * 1200,
        )
        deleted += sum(a["type"] == "delete_file" for a in actions)
    assert deleted <= 1
