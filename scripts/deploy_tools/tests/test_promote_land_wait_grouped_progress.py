"""`land-wait` groups its pending Kustomizations by reason (#7894).

A broad promotion printed `name (at <sha>, before this merge)` once per Kustomization, about
seventy times on one line. The line now names the shared reason once with a count, and names
only the Kustomizations held for some other reason.

Run: uv run pytest scripts/deploy_tools/tests/test_promote_land_wait_grouped_progress.py
"""

import re

from _land_wait_fakes import M, P, Sim, at, digests, kustomization
from deploy_tools.promote_lib import wait as promote_wait

BEHIND = [f"svc{i:02d}" for i in range(1, 7)]
NOT_READY = "svc07"
# BEHIND and `homelab`, every one applied at P.
BEHIND_COUNT = len(BEHIND) + 1
PUBLISHED = {
    M: digests(**{n: "2" for n in (*BEHIND, NOT_READY)}),
    P: digests(**{n: "1" for n in (*BEHIND, NOT_READY)}),
}


def _progress(capsys) -> list[str]:
    return [
        line
        for line in capsys.readouterr().out.splitlines()
        if "promote.py land-wait:" in line
    ]


def _names_behind(line: str) -> list[str]:
    return [n for n in (*BEHIND, "homelab") if re.search(rf"\b{n}\b", line)]


def test_a_shared_reason_prints_once_with_a_count_and_names_only_the_minority(capsys):
    stuck = kustomization(NOT_READY, f"master@sha1:{M}", ready=False)
    sim = Sim(polls=[[*at(P, *BEHIND), stuck]], published=PUBLISHED)
    assert sim.wait().verdict == "gave-up"
    lines = _progress(capsys)
    assert len(lines) == 1, lines
    assert not _names_behind(lines[0]), lines[0]
    assert re.search(rf"\b{BEHIND_COUNT}\b", lines[0]), lines[0]
    assert re.search(rf"not Ready\W+{NOT_READY}|{NOT_READY}\W+not Ready", lines[0])


def test_land_wait_prints_one_grouped_line_per_state_change(capsys):
    """The not-Ready group clears on the third poll, and the promotion covers M on the fifth."""
    stuck = kustomization(NOT_READY, f"master@sha1:{M}", ready=False)
    ready = kustomization(NOT_READY, f"master@sha1:{M}")
    sim = Sim(
        polls=[
            [*at(P, *BEHIND), stuck],
            [*at(P, *BEHIND), stuck],
            [*at(P, *BEHIND), ready],
            [*at(P, *BEHIND), ready],
            at(M, *BEHIND, NOT_READY),
        ],
        published=PUBLISHED,
    )
    assert sim.wait().verdict == "settled"
    lines = _progress(capsys)
    assert len(lines) == 2, lines
    assert [_names_behind(line) for line in lines] == [[], []], lines
    assert NOT_READY in lines[0] and NOT_READY not in lines[1], lines


def test_a_widened_wait_groups_every_name_by_reason(capsys):
    """An unreadable ancestor widens the set to every name; the line still counts them."""
    sim = Sim(
        polls=[at(P, *BEHIND, NOT_READY)],
        published={
            **PUBLISHED,
            P: promote_wait.WaitReadError("sha-P holds no digests.json"),
        },
    )
    result = sim.wait()
    assert result.verdict == "gave-up" and result.widened
    lines = _progress(capsys)
    assert len(lines) == 1, lines
    named = [
        n for n in (*BEHIND, NOT_READY, "homelab") if re.search(rf"\b{n}\b", lines[0])
    ]
    assert not named, lines[0]
    assert re.search(rf"\b{BEHIND_COUNT + 1}\b", lines[0]), lines[0]
