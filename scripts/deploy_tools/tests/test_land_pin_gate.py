"""The pin gate before a forward-only pin bump merges, and `land.sh --regate` (H1.1g).

Every boundary is a fake (`_land_fakes.Fakes`): the pins at each SHA, DEPLOY_HOLD, `last-good`,
the gate run and every `gh` call. Nothing here posts a status or merges anything.

Run: uv run pytest scripts/deploy_tools/tests/test_land_pin_gate.py
"""

import pytest
from _land_fakes import LAST_GOOD, Fakes
from deploy_tools.land_lib import merge, pin_gate
from deploy_tools.land_lib.outcome import Outcome
from deploy_tools.land_lib.pin_reads import Unreadable

HEAD = "c" * 40
BASE = "prbase"  # what the fake `git merge-base` answers
OTHER = "d" * 40
LONGHORN = "k3s_longhorn_version"
STATUS_URL = f"repos/{{owner}}/{{repo}}/statuses/{HEAD}"
MERGE_URL = "repos/{owner}/{repo}/pulls/999/merge"

# A PR moving Longhorn one minor, over a `last-good` that holds the base's value.
BUMP = {
    BASE: {LONGHORN: "v1.12.1"},
    HEAD: {LONGHORN: "v1.13.0"},
    LAST_GOOD: {LONGHORN: "v1.12.1"},
}


def _open_then_merged() -> dict:
    return {
        "state,mergeable,headRefOid": [
            {"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": HEAD},
            {"state": "MERGED", "mergeable": "MERGEABLE", "headRefOid": HEAD},
        ]
    }


def _merge(landing, **fakes):
    """Run the merge wait over a green PR left for a direct merge; the calls it made."""
    fakes.setdefault("pins", BUMP)
    ln, calls = landing(Fakes(gh_views=_open_then_merged(), **fakes))
    ln.direct_merge_subject = "Bump Longhorn to v1.13.0"
    merge.await_merge(ln)
    return calls


def _refused(landing, **fakes) -> tuple[Outcome, list]:
    fakes.setdefault("pins", BUMP)
    ln, calls = landing(Fakes(gh_views=_open_then_merged(), **fakes))
    ln.direct_merge_subject = "Bump Longhorn to v1.13.0"
    with pytest.raises(Outcome) as exc:
        merge.await_merge(ln)
    assert exc.value.rc == 1
    return exc.value, calls


def _gh_urls(calls) -> list[str]:
    return [c[1][3] for c in calls if c[0] == "gh"]


def _names(calls) -> list[str]:
    return [c[0] for c in calls]


# ── the merge path ──────────────────────────────────────────────────────────────────────────


def test_a_pr_that_bumps_no_pin_merges_with_no_gate_and_no_status(landing):
    calls = _merge(landing, pins={})
    assert _gh_urls(calls) == [MERGE_URL]
    assert not {"hold", "last_good", "run_gates"} & set(_names(calls))


def test_a_bump_with_no_hold_and_none_in_flight_posts_the_status_then_merges(landing):
    calls = _merge(landing)
    assert _gh_urls(calls) == [STATUS_URL, MERGE_URL]
    post = next(c[1] for c in calls if c[0] == "gh")
    assert "context=homelab/upgrade-gates" in post
    assert "state=success" in post
    [run] = [c[1] for c in calls if c[0] == "run_gates"]
    assert run == ("longhorn-upgrade",)


def test_a_metallb_bump_runs_the_metallb_gate_set(landing):
    pins = {
        BASE: {"k3s_metallb_version": "v0.15.2"},
        HEAD: {"k3s_metallb_version": "v0.15.3"},
        LAST_GOOD: {"k3s_metallb_version": "v0.15.2"},
    }
    calls = _merge(landing, pins=pins)
    assert [c[1] for c in calls if c[0] == "run_gates"] == [("metallb-upgrade",)]


@pytest.mark.parametrize(
    ("hold", "why"),
    [
        (None, "does not exist"),
        ("", "is empty"),
        ("   ", "is empty"),
        ("a" * 40, "holds"),
    ],
    ids=["missing", "empty", "blank", "a-sha"],
)
def test_a_bump_is_refused_unless_deploy_hold_reads_none(landing, hold, why):
    outcome, calls = _refused(landing, deploy_hold=hold)
    assert why in outcome.error
    assert "run_gates" not in _names(calls)
    assert _gh_urls(calls) == []


def test_a_failed_hold_read_refuses_the_bump(landing):
    outcome, calls = _refused(landing, deploy_hold=Unreadable("gh: HTTP 502"))
    assert "HTTP 502" in outcome.error
    assert _gh_urls(calls) == []


def test_a_bump_is_refused_while_the_range_already_changes_that_pin(landing):
    pins = {**BUMP, OTHER: {LONGHORN: "v1.13.0"}}
    outcome, calls = _refused(landing, pins=pins, walk=[OTHER], tip=OTHER)
    assert f"already changes it at {OTHER[:9]}" in outcome.error
    assert "run_gates" not in _names(calls)
    assert _gh_urls(calls) == []


def test_a_range_that_changes_only_the_other_pin_passes(landing):
    pins = {
        **BUMP,
        OTHER: {LONGHORN: "v1.12.1", "k3s_metallb_version": "v0.15.3"},
    }
    calls = _merge(landing, pins=pins, walk=[OTHER], tip=OTHER)
    assert _gh_urls(calls) == [STATUS_URL, MERGE_URL]


@pytest.mark.parametrize(
    "last_good", [None, Unreadable("GHCR: HTTP 503")], ids=["absent", "unreadable"]
)
def test_a_bump_is_refused_when_last_good_cannot_be_read(landing, last_good):
    outcome, calls = _refused(landing, last_good=last_good)
    assert "last-good" in outcome.error
    assert _gh_urls(calls) == []


def test_a_last_good_off_master_refuses_the_bump(landing):
    outcome, _ = _refused(landing, is_ancestor_of={HEAD: 1, "0" * 40: 1}, tip="0" * 40)
    assert "not an ancestor" in outcome.error


def test_a_bump_is_refused_on_any_host_but_daniel_box(landing):
    outcome, calls = _refused(landing, hostname="daniel-server")
    assert "run land.sh on daniel-box" in outcome.error
    assert "hold" not in _names(calls)


def test_a_failing_gate_refuses_the_merge_and_posts_nothing(landing):
    outcome, calls = _refused(landing, gates=[(2, "gate 2 FAILED — a restore drill")])
    assert "exited 2" in outcome.error
    assert _gh_urls(calls) == []


def test_a_failed_status_post_refuses_the_merge(landing):
    outcome, calls = _refused(landing, gh_merge_rc=[1])
    assert "posting homelab/upgrade-gates" in outcome.error
    assert _gh_urls(calls) == [STATUS_URL]


# ── the status description ──────────────────────────────────────────────────────────────────


def test_the_description_names_who_ran_which_set_and_the_last_line():
    text = pin_gate.description(
        "longhorn-upgrade",
        "gate 1 ok\nall 4 gates passed",
        "land.sh as ubuntu@daniel-box",
    )
    assert text == "land.sh as ubuntu@daniel-box: longhorn-upgrade: all 4 gates passed"


def test_a_long_description_is_cut_to_what_github_accepts():
    text = pin_gate.description("longhorn-upgrade", "x" * 400, "land.sh as u@h")
    assert len(text) == pin_gate.DESCRIPTION_MAX
    assert text.endswith("…")


# ── --regate ────────────────────────────────────────────────────────────────────────────────


def _merged() -> dict:
    return {"state,headRefOid": {"state": "MERGED", "headRefOid": HEAD}}


def test_regate_posts_the_status_for_a_merged_bump_and_never_merges(landing):
    ln, calls = landing(Fakes(gh_views=_merged(), pins=BUMP, deploy_hold="a" * 40))
    pin_gate.regate(ln)
    assert _gh_urls(calls) == [STATUS_URL]
    assert not {"hold", "last_good"} & set(_names(calls))


def test_regate_refuses_a_pr_that_is_not_merged(landing):
    views = {"state,headRefOid": {"state": "OPEN", "headRefOid": HEAD}}
    ln, calls = landing(Fakes(gh_views=views, pins=BUMP))
    with pytest.raises(Outcome) as exc:
        pin_gate.regate(ln)
    assert "is OPEN" in exc.value.error
    assert _gh_urls(calls) == []


def test_regate_refuses_a_pr_that_bumps_no_pin(landing):
    ln, calls = landing(Fakes(gh_views=_merged(), pins={}))
    with pytest.raises(Outcome) as exc:
        pin_gate.regate(ln)
    assert "bumps no forward-only pin" in exc.value.error
    assert _gh_urls(calls) == []


def test_land_sh_regate_exits_0_and_runs_no_landing(land_run):
    rc, out, _, calls, logline = land_run(
        ["--regate", "999"], Fakes(gh_views=_merged(), pins=BUMP)
    )
    assert rc == 0
    assert _gh_urls(calls) == [STATUS_URL]
    assert not {"await_ci", "tick", "deploy"} & set(_names(calls))
    assert logline == ""
    assert "VERDICT" not in out
