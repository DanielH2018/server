"""The words `land_result.py` restates from `outcome.py`, and the `next` field it reads.

Run: uv run pytest scripts/dev/tests/fanout/test_fanout_land_result.py
"""

from deploy_tools.land_lib.detach import result_fields
from deploy_tools.land_lib.ledger import Ledger, result_line
from deploy_tools.land_lib.outcome import NEXT, Cause, Next, Outcome, Verdict
from deploy_tools.promote_lib import wait
from dev.fanout_lib import land_result


def test_every_word_land_result_restates_is_the_one_its_source_defines():
    restated = [
        (land_result.NEXT_DONE, Next.DONE),
        (land_result.NEXT_RERUN, Next.RERUN),
        (land_result.NEXT_FOLLOW_LAND, Next.FOLLOW_LAND),
        (land_result.REFUSED, Verdict.REFUSED),
        (land_result.PROMOTE_HELD, Cause.PROMOTE_HELD),
        (land_result.HOLD_OWN, wait.HOLD_OWN),
        (land_result.HOLD_OTHER, wait.HOLD_OTHER),
    ]
    assert [(word, source) for word, source in restated if word != source] == []


def test_the_finished_verdicts_are_the_verdicts_whose_next_is_done():
    done = {
        v for (v, when), action in NEXT.items() if when is None and action == Next.DONE
    }
    assert land_result.FINISHED_VERDICTS == done


def test_a_refusal_before_the_merge_parks_with_the_command_that_re_lands_it():
    refused = Outcome(75, "the reboot window bars a k8s PR", "refused")
    line = result_line(Ledger(pr="4000", t_start=0.0), refused)
    parked = land_result.park_line(result_fields(line) or {})
    assert parked.startswith(
        "parked: refused before merge (a landing rule); re-land with "
    )
    assert parked.endswith(land_result.reland_command("4000"))


def test_a_deferral_the_gate_gave_up_on_is_not_finished_though_its_verdict_is():
    result = {"verdict": "deferred", "cause": "gate-unreadable", "next": "rerun"}
    assert not land_result.finished(result)
    assert land_result.finished({"verdict": "deferred"})
