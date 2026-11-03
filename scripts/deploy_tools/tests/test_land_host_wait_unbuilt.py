"""#7024: `wait_for_hosts` does not defer on a host's updater when the builder never published it.

The builder's series are served through `HostReads.fetch`, the Prometheus boundary the wait
already crosses: `homelab_repo_published_info{package,sha,version}` and
`homelab_builder_unbuilt_since_timestamp_seconds{reason,sha}`, the series `probe.py builder`
reads from daniel-box's textfiles. The fake matches a query by the metric name it contains, so
any selector or aggregation over those series gets the samples.

Run: uv run pytest scripts/deploy_tools/tests/test_land_host_wait_unbuilt.py
"""

import json
import urllib.parse
from datetime import UTC, datetime
from types import SimpleNamespace

from _land_fakes import make_landing
from deploy_tools.land_lib import host_wait
from deploy_tools.land_lib.host_wait import HostReads, HostTarget
from deploy_tools.land_lib.outcome import Outcome, Verdict
from lib.repo_paths import REPO
from diagnostics.probe_lib.builder import fetch_deadline_s
from lib.systemd_timer import timer_settings

MERGE = "a" * 40
BEFORE = "c" * 12  # an ancestor of MERGE: what the host runs and the stale publish
AFTER = "d" * 12  # a descendant of MERGE
PI_DROPIN = (REPO / "hosts/daniel-pi/units/homelab-updater.timer.conf").read_text()
QUARTER_HOUR_DROPIN = (
    REPO / "hosts/lib/tests/container/fixture/units/updater-timer.conf"
).read_text()
SHARED_TIMER = (REPO / host_wait.SHARED_TIMER).read_text()
SCRAPE = {"origin": "daniel-box", "instance": "daniel-box:9100", "job": "node"}


def _at(hour: int, minute: int) -> float:
    return datetime(2026, 11, 2, hour, minute, tzinfo=UTC).timestamp()


def _pi(dropin: str) -> HostTarget:
    triggers, delay = timer_settings(SHARED_TIMER, [dropin])
    return HostTarget("daniel-pi", armed=True, triggers=triggers, delay=delay)


def _git(*args, cwd=None, check=True):
    """`merge-base --is-ancestor MERGE <sha>` holds only for AFTER and MERGE itself."""
    descends = args[0] == "merge-base" and args[-1] in (AFTER, MERGE[:12], MERGE)
    return SimpleNamespace(returncode=0 if descends else 1, stdout="")


def _published(package: str, sha: str) -> dict:
    labels = {"package": package, "sha": sha, "version": f"0.6283+g{sha}"}
    return {"metric": SCRAPE | labels, "value": [0, "1"]}


def _fetch(published: list[dict], reason: str = "build"):
    """Prometheus: daniel-pi applied BEFORE, and the builder published `published`."""
    host = {"host": "daniel-pi"}
    series = {
        host_wait.APPLIED: [{"metric": host | {"sha": BEFORE}, "value": [0, "1"]}],
        host_wait.RECONCILE_OK: [{"metric": host, "value": [0, "1"]}],
        host_wait.LAST_SUCCESS: [{"metric": host, "value": [0, str(_at(3, 6))]}],
        "homelab_repo_published_info": published,
        "homelab_builder_unbuilt_since_timestamp_seconds": [
            {
                "metric": SCRAPE | {"reason": reason, "sha": BEFORE},
                "value": [0, str(_at(18, 25))],
            }
        ],
    }

    def fetch(url: str, timeout: float) -> bytes:
        promql = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["query"][0]
        result = [s for name, rows in series.items() if name in promql for s in rows]
        return json.dumps({"status": "success", "data": {"result": result}}).encode()

    return fetch


def _clock(*times: float):
    """A clock that reads each of `times` in turn, then keeps reading the last."""
    seen = list(times)

    def clock() -> float:
        return seen.pop(0) if len(seen) > 1 else seen[0]

    return clock


def _wait(target: HostTarget, fetch, clock, merged_at: float) -> Outcome | None:
    ln, _ = make_landing()
    reads = HostReads(fetch=fetch, git=_git, clock=clock, sleep=lambda s: None)
    try:
        host_wait.wait_for_hosts(ln, [target], MERGE, merged_at, reads=reads)
    except Outcome as outcome:
        return outcome
    return None


def _ends_with(published: list[dict], reason: str = "build") -> Outcome | None:
    """The 12:00 merge on the Pi's daily schedule, whose next fire is past the 45-minute cap."""
    return _wait(
        _pi(PI_DROPIN), _fetch(published, reason), _clock(_at(12, 0)), _at(12, 0)
    )


def test_a_publish_predating_the_merge_is_not_deferred_and_one_containing_it_is():
    stale = _ends_with([_published("homelab-daniel-pi", BEFORE)])
    assert stale is not None
    assert stale.verdict != Verdict.DEFERRED
    assert stale.verdict in {v.value for v in Verdict}

    for sha in (MERGE[:12], AFTER):
        built = _ends_with([_published("homelab-daniel-pi", sha)])
        assert built is not None
        assert (built.verdict, built.rc) == ("deferred", 75)
        assert "next updater fire is at 2026-11-03 03:05 UTC" in built.detail


def test_the_unbuilt_detail_names_the_builder_reason_and_the_last_published_version():
    outcome = _ends_with([_published("homelab-daniel-pi", BEFORE)], reason="tools")
    assert outcome is not None and outcome.verdict != Verdict.DEFERRED
    assert "tools" in outcome.detail
    assert f"0.6283+g{BEFORE}" in outcome.detail


def test_another_host_package_published_at_the_merge_does_not_count_for_the_pi():
    outcome = _ends_with(
        [
            _published("homelab-daniel-box", MERGE[:12]),
            _published("homelab-daniel-pi", BEFORE),
        ]
    )
    assert outcome is not None
    assert outcome.verdict != Verdict.DEFERRED


def test_a_host_past_its_deadline_with_nothing_published_is_not_deferred():
    # A 12:00 merge on the quarter-hour schedule has its deadline at 12:31, inside the cap,
    # so the wait polls and ends at the deadline rather than on the first query.
    outcome = _wait(
        _pi(QUARTER_HOUR_DROPIN),
        _fetch([_published("homelab-daniel-pi", BEFORE)]),
        _clock(_at(12, 0), _at(12, 30), _at(12, 32)),
        _at(12, 0),
    )
    assert outcome is not None
    assert outcome.verdict != Verdict.DEFERRED
    assert f"0.6283+g{BEFORE}" in outcome.detail


def test_a_healthy_builder_that_has_not_reached_the_merge_stays_deferred():
    for reason in ("ok", "canary", "no-green"):
        outcome = _ends_with([_published("homelab-daniel-pi", BEFORE)], reason=reason)
        assert outcome is not None
        assert (outcome.verdict, outcome.rc) == ("deferred", 75)


def test_no_published_series_for_the_host_stays_deferred():
    outcome = _ends_with([_published("homelab-daniel-box", BEFORE)])
    assert outcome is not None
    assert outcome.verdict == Verdict.DEFERRED


def test_a_failed_read_of_the_published_versions_stays_deferred():
    answer = _fetch([_published("homelab-daniel-pi", BEFORE)])

    def fetch(url: str, timeout: float) -> bytes:
        if "homelab_repo_published_info" in urllib.parse.unquote(url):
            raise OSError("connection reset")
        return answer(url, timeout)

    outcome = _wait(_pi(PI_DROPIN), fetch, _clock(_at(12, 0)), _at(12, 0))
    assert outcome is not None
    assert outcome.verdict == Verdict.DEFERRED


def test_an_unbuilt_host_ends_the_landing_unbuilt_at_exit_75():
    outcome = _ends_with([_published("homelab-daniel-pi", BEFORE)])
    assert outcome is not None
    assert (outcome.verdict, outcome.rc) == ("unbuilt", 75)


def _builder_runs(fetches: list[float], publishes: list[list[dict]]):
    """The red fake, with the builder's last fetch and its publishes read in turn per query."""
    stale = _fetch([_published("homelab-daniel-pi", BEFORE)])

    def fetch(url: str, timeout: float) -> bytes:
        promql = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["query"][0]
        if "homelab_builder_fetch_timestamp_seconds" in promql:
            at = fetches.pop(0) if len(fetches) > 1 else fetches[0]
            result = [{"metric": SCRAPE, "value": [0, str(at)]}]
        elif "homelab_repo_published_info" in promql:
            result = publishes.pop(0) if len(publishes) > 1 else publishes[0]
        else:
            return stale(url, timeout)
        return json.dumps({"status": "success", "data": {"result": result}}).encode()

    return fetch


def _wait_counting_sleeps(fetch, clock) -> tuple[Outcome | None, int]:
    sleeps = []
    ln, _ = make_landing()
    reads = HostReads(fetch=fetch, git=_git, clock=clock, sleep=sleeps.append)
    try:
        host_wait.wait_for_hosts(ln, [_pi(PI_DROPIN)], MERGE, _at(12, 0), reads=reads)
    except Outcome as outcome:
        return outcome, len(sleeps)
    return None, len(sleeps)


def test_a_failing_reason_fetched_before_the_merge_waits_for_the_builder_s_next_run():
    stale = [_published("homelab-daniel-pi", BEFORE)]
    built = [_published("homelab-daniel-pi", AFTER)]
    fixed = _builder_runs([_at(11, 58), _at(12, 2)], [stale, built])
    outcome, sleeps = _wait_counting_sleeps(fixed, _clock(_at(12, 0)))
    assert sleeps >= 1
    assert outcome is not None and outcome.verdict == Verdict.DEFERRED

    still_failing = _builder_runs([_at(11, 58), _at(12, 2)], [stale])
    outcome, sleeps = _wait_counting_sleeps(still_failing, _clock(_at(12, 0)))
    assert sleeps >= 1
    assert outcome is not None and outcome.verdict == Verdict.UNBUILT


def test_a_builder_that_never_fetches_again_ends_unbuilt_past_its_fetch_deadline():
    never = _builder_runs([_at(11, 58)], [[_published("homelab-daniel-pi", BEFORE)]])
    past = _at(12, 0) + fetch_deadline_s() + 1
    outcome, _ = _wait_counting_sleeps(never, _clock(_at(12, 0), past))
    assert outcome is not None and outcome.verdict == Verdict.UNBUILT
