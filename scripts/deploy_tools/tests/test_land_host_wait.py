"""H3.9a's host wait: `host_targets`, the Prometheus transport, the settle rule and the deadline.

Nothing calls `host_wait` yet. Every boundary is a field of `HostReads`, so no test renders in
a real snapshot or reaches a live Prometheus. The schedule tests read the Pi's real drop-in and
the shared timer from `hosts/`.

Run: uv run pytest scripts/deploy_tools/tests/test_land_host_wait.py
"""

import json
import urllib.error
import urllib.parse
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from _land_fakes import make_landing
from deploy_tools.land_lib import host_wait
from deploy_tools.land_lib.host_wait import HostReads, HostState, HostTarget
from deploy_tools.land_lib.outcome import Outcome
from lib.repo_paths import REPO
from lib.systemd_timer import timer_settings

MERGE = "a" * 40
PARENT = "b" * 40
LINK = "root/usr/lib/systemd/system/timers.target.wants/homelab-updater.timer"
PI_DROPIN = (REPO / "hosts/daniel-pi/units/homelab-updater.timer.conf").read_text()
SHARED_TIMER = (REPO / host_wait.SHARED_TIMER).read_text()


def _at(hour: int, minute: int) -> float:
    return datetime(2026, 10, 17, hour, minute, tzinfo=UTC).timestamp()


def _pi(armed: bool = True) -> HostTarget:
    triggers, delay = timer_settings(SHARED_TIMER, [PI_DROPIN])
    return HostTarget("daniel-pi", armed=armed, triggers=triggers, delay=delay)


def _git(
    returncodes: dict[str, int] | None = None, stdout: dict[str, str] | None = None
):
    """A `git` whose exit code and output are keyed by its subcommand."""
    returncodes = returncodes or {}
    stdout = {"rev-parse": PARENT, **(stdout or {})}

    def git(*args, cwd=None, check=True):
        return SimpleNamespace(
            returncode=returncodes.get(args[0], 0), stdout=stdout.get(args[0], "")
        )

    return git


# -- host_targets ------------------------------------------------------------------------


def _snapshots(tmp_path: Path, hosts: dict[str, list[str]], failed: str = ""):
    """Snapshot roots holding `hosts/<host>/package.yml` for each host listed per commit."""
    roots = {}
    for sha, names in hosts.items():
        root = tmp_path / sha[:1]
        for name in names:
            (root / "hosts" / name).mkdir(parents=True)
            (root / "hosts" / name / "package.yml").write_text("{}\n")
        (root / host_wait.SHARED_TIMER).parent.mkdir(parents=True, exist_ok=True)
        (root / host_wait.SHARED_TIMER).write_text(SHARED_TIMER)
        roots[sha] = root

    @contextmanager
    def snapshot(primary, sha):
        yield None if sha == failed else roots[sha]

    return snapshot, roots


def _reads(snapshot, trees: Mapping[tuple[str, str], Any], **kw) -> HostReads:
    """Reads whose render answers `trees[(commit letter, host)]`."""
    return HostReads(
        snapshot=snapshot,
        render=lambda root, host: trees[(root.name, host)],
        git=kw.pop("git", _git()),
        **kw,
    )


def _dropin(text: str = PI_DROPIN) -> dict:
    return {str(host_wait.DROPIN): ("file", 0o644, text.encode())}


def test_a_host_whose_render_differs_is_a_target_and_one_that_matches_is_not(tmp_path):
    snapshot, _ = _snapshots(
        tmp_path,
        {MERGE: ["daniel-pi", "daniel-box"], PARENT: ["daniel-pi", "daniel-box"]},
    )
    same = {"nfpm.yaml": ("file", 0o644, b"x")}
    trees = {
        ("a", "daniel-pi"): {"nfpm.yaml": ("file", 0o644, b"new")},
        ("b", "daniel-pi"): {"nfpm.yaml": ("file", 0o644, b"old")},
        ("a", "daniel-box"): same,
        ("b", "daniel-box"): same,
    }
    targets = host_wait.host_targets(Path("/p"), MERGE, _reads(snapshot, trees))
    assert [t.host for t in targets] == ["daniel-pi"]


def test_a_mode_change_alone_makes_a_target(tmp_path):
    snapshot, _ = _snapshots(tmp_path, {MERGE: ["daniel-pi"], PARENT: ["daniel-pi"]})
    trees = {
        ("a", "daniel-pi"): {"x": ("file", 0o755, b"x")},
        ("b", "daniel-pi"): {"x": ("file", 0o644, b"x")},
    }
    assert host_wait.host_targets(Path("/p"), MERGE, _reads(snapshot, trees))


@pytest.mark.parametrize("failed", [MERGE, PARENT])
def test_a_failed_snapshot_makes_every_host_at_the_merge_commit_a_target(
    tmp_path, failed
):
    snapshot, _ = _snapshots(tmp_path, {MERGE: [], PARENT: []}, failed=failed)
    git = _git(
        returncodes={"cat-file": 0},
        stdout={"ls-tree": "hosts/daniel-pi\nhosts/bootstrap-host.sh\n"},
    )
    calls = []

    def recording_git(*args, cwd=None, check=True):
        calls.append(args)
        if args[0] == "cat-file" and "bootstrap-host.sh" in args[2]:
            return SimpleNamespace(returncode=128, stdout="")
        return git(*args, cwd=cwd, check=check)

    reads = _reads(snapshot, {}, git=recording_git)
    targets = host_wait.host_targets(Path("/p"), MERGE, reads)
    assert targets == [HostTarget("daniel-pi")]
    assert ("ls-tree", "--name-only", MERGE, "hosts/") in calls


def test_a_failed_render_makes_every_host_a_target(tmp_path):
    snapshot, _ = _snapshots(tmp_path, {MERGE: ["daniel-pi"], PARENT: ["daniel-pi"]})
    git = _git(stdout={"ls-tree": "hosts/daniel-pi\n"})
    trees = {("a", "daniel-pi"): None, ("b", "daniel-pi"): {}}
    targets = host_wait.host_targets(
        Path("/p"), MERGE, _reads(snapshot, trees, git=git)
    )
    assert targets == [HostTarget("daniel-pi")]


def test_the_enable_link_is_read_from_the_first_parent_not_the_merge(tmp_path):
    snapshot, _ = _snapshots(tmp_path, {MERGE: ["daniel-pi"], PARENT: ["daniel-pi"]})
    link = {LINK: ("link", 0, b"../homelab-updater.timer")}
    trees = {("a", "daniel-pi"): _dropin() | link, ("b", "daniel-pi"): _dropin()}
    [target] = host_wait.host_targets(Path("/p"), MERGE, _reads(snapshot, trees))
    assert target.armed is False
    assert target.triggers == {"OnCalendar": ["*-*-* 01..03,06..22:05/15:00"]}

    trees = {("a", "daniel-pi"): _dropin(), ("b", "daniel-pi"): _dropin() | link}
    [target] = host_wait.host_targets(Path("/p"), MERGE, _reads(snapshot, trees))
    assert target.armed is True


def test_a_host_the_merge_adds_is_a_target_with_nothing_armed(tmp_path):
    snapshot, _ = _snapshots(tmp_path, {MERGE: ["daniel-pi"], PARENT: []})
    trees = {("a", "daniel-pi"): _dropin()}
    [target] = host_wait.host_targets(Path("/p"), MERGE, _reads(snapshot, trees))
    assert (target.host, target.armed, target.triggers) == ("daniel-pi", False, None)


def test_a_dangling_enable_link_written_to_disk_reads_as_armed(tmp_path):
    # `write_packages` writes the link pointing at the updater's unit, absent from this root.
    wants = tmp_path / "root/usr/lib/systemd/system/timers.target.wants"
    wants.mkdir(parents=True)
    (wants / "homelab-updater.timer").symlink_to("../homelab-updater.timer")
    tree = host_wait.read_host_tree(tmp_path)
    assert tree[LINK] == ("link", 0, b"../homelab-updater.timer")
    assert host_wait._target("daniel-pi", tree, REPO).armed is True


def test_the_real_pi_render_reads_its_drop_in_schedule():
    # A real render through the CLI, from this checkout. `armed` is left alone: H3.9b ships
    # the link.
    tree = host_wait.render_host_tree(REPO, "daniel-pi")
    assert tree is not None
    target = host_wait._target("daniel-pi", tree, REPO)
    assert target.triggers == {"OnCalendar": ["*-*-* 01..03,06..22:05/15:00"]}


def test_the_git_fallback_finds_the_pi_at_head():
    hosts = host_wait._every_host(REPO, "HEAD", host_wait.REAL_READS)
    assert HostTarget("daniel-pi") in hosts


# -- the transport -----------------------------------------------------------------------


def _prom(
    series: dict[str, list[dict]] | None = None,
    loopback: Exception | None = None,
    cluster: Exception | None = None,
):
    """A `fetch` answering each metric's query from `series`, raising per address."""
    urls = []

    def fetch(url: str, timeout: float) -> bytes:
        urls.append(url)
        failure = loopback if "//127.0.0.1:" in url else cluster
        if failure is not None:
            raise failure
        promql = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["query"][0]
        metric = promql.split("{")[0]
        result = (series or {}).get(metric, [])
        return json.dumps({"status": "success", "data": {"result": result}}).encode()

    return fetch, urls


REFUSED = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))


def test_the_clusterip_is_read_from_the_observability_defaults():
    address = host_wait.prometheus_cluster_ip()
    assert address is not None and address.count(".") == 3


def test_a_refused_loopback_falls_back_to_the_clusterip(tmp_path):
    defaults = tmp_path / "main.yml"
    defaults.write_text("observability_prometheus_cluster_ip: 10.43.0.9\n")
    fetch, urls = _prom(loopback=REFUSED)
    reads = HostReads(fetch=fetch, defaults=defaults)
    assert host_wait.query("up", reads) == []
    assert [u.split("/api")[0] for u in urls] == [
        "http://127.0.0.1:9090",
        "http://10.43.0.9:9090",
    ]


def test_an_answering_loopback_never_asks_the_clusterip(tmp_path):
    fetch, urls = _prom()
    host_wait.query("up", HostReads(fetch=fetch))
    assert len(urls) == 1 and "//127.0.0.1:9090/" in urls[0]


def test_both_refused_is_prometheus_unreachable(tmp_path):
    fetch, _ = _prom(loopback=REFUSED, cluster=urllib.error.URLError(TimeoutError()))
    with pytest.raises(
        host_wait.PrometheusUnreachable,
        match="Prometheus unreachable on loopback and ClusterIP",
    ):
        host_wait.query("up", HostReads(fetch=fetch))


# -- the settle rule ---------------------------------------------------------------------


def test_the_merge_sha_with_reconcile_at_one_is_settled():
    state = HostState(applied=[MERGE[:12]], reconcile_ok=1)
    assert host_wait.settled(state, MERGE, Path("/p"), HostReads(git=_git()))


def test_a_descendant_with_reconcile_at_one_is_settled():
    state = HostState(applied=["c" * 12], reconcile_ok=1)
    reads = HostReads(git=_git(returncodes={"merge-base": 0}))
    assert host_wait.settled(state, MERGE, Path("/p"), reads)


@pytest.mark.parametrize(
    "state",
    [
        None,
        HostState(applied=["c" * 12], reconcile_ok=1),
        HostState(applied=[MERGE[:12]], reconcile_ok=None),
        HostState(applied=[], reconcile_ok=1),
    ],
    ids=["absent", "older-commit", "no-reconcile-series", "no-applied-series"],
)
def test_an_older_commit_or_an_absent_series_is_not_settled(state):
    reads = HostReads(git=_git(returncodes={"merge-base": 1}))
    assert not host_wait.settled(state, MERGE, Path("/p"), reads)


@pytest.mark.parametrize("label", ["--output=x", "ABCDEF1"])
def test_a_label_that_is_not_lowercase_hex_is_not_settled_and_git_is_never_called(
    label,
):
    calls = []

    def recording_git(*args, cwd=None, check=True):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="")

    state = HostState(applied=[label], reconcile_ok=1)
    reads = HostReads(git=recording_git)
    assert not host_wait.settled(state, MERGE, Path("/p"), reads)
    assert calls == []


# -- wait_for_hosts ----------------------------------------------------------------------


def _series(sha: str = MERGE[:12], ok: str = "1", success: str = "1760700000"):
    labels = {"host": "daniel-pi"}
    return {
        host_wait.APPLIED: [{"metric": labels | {"sha": sha}, "value": [0, "1"]}],
        host_wait.RECONCILE_OK: [{"metric": labels, "value": [0, ok]}],
        host_wait.LAST_SUCCESS: [{"metric": labels, "value": [0, success]}],
    }


def _wait(target, fetch, clock, merged_at=None):
    """Run the wait to its end; return the Outcome, or None when it returned, and the sleeps."""
    ln, _ = make_landing()
    sleeps = []
    times = iter(clock)
    reads = HostReads(
        fetch=fetch,
        git=_git(returncodes={"merge-base": 1}),
        clock=lambda: next(times),
        sleep=sleeps.append,
    )
    try:
        host_wait.wait_for_hosts(
            ln, [target], MERGE, merged_at or clock[0], reads=reads
        )
    except Outcome as outcome:
        return outcome, sleeps
    return None, sleeps


def test_a_host_settled_on_the_first_query_never_waits():
    fetch, _ = _prom(_series())
    outcome, sleeps = _wait(_pi(armed=False), fetch, [_at(12, 0)])
    assert outcome is None and sleeps == []


def test_a_held_host_ends_deploy_failed_naming_the_clear_hold_command():
    fetch, _ = _prom(_series(sha="c" * 12, ok="0"))
    outcome, _ = _wait(_pi(), fetch, [_at(12, 0)])
    assert (outcome.verdict, outcome.rc) == ("deploy-failed", 1)
    assert "daniel-pi" in outcome.detail
    assert "sudo /usr/lib/homelab/homelab-updater --clear-hold" in outcome.detail


def test_a_first_parent_with_no_enable_link_ends_deferred_naming_the_start_command():
    fetch, _ = _prom(_series(sha="c" * 12))
    outcome, sleeps = _wait(_pi(armed=False), fetch, [_at(12, 0)])
    assert (outcome.verdict, outcome.rc) == ("deferred", 75)
    assert "no timer is armed" in outcome.detail
    assert "sudo systemctl start homelab-updater.service" in outcome.detail
    assert sleeps == []


def test_a_mid_day_merge_waits_until_the_host_applies():
    stale, _ = _prom(_series(sha="c" * 12))
    fresh, _ = _prom(_series())
    answers = iter([stale] * 3 + [fresh] * 3)
    outcome, sleeps = _wait(
        _pi(),
        lambda url, timeout: next(answers)(url, timeout),
        [_at(12, 0), _at(12, 1)],
    )
    assert outcome is None and sleeps == [host_wait.POLL_S]


def test_a_merge_in_the_pi_quiet_hours_ends_deferred_naming_the_next_fire():
    # 22:50 is the last fire before the 23:00 to 00:59 gap, so the second fire is 01:20.
    fetch, _ = _prom(_series(sha="c" * 12))
    outcome, sleeps = _wait(_pi(), fetch, [_at(22, 55)])
    assert (outcome.verdict, outcome.rc) == ("deferred", 75)
    assert "next updater fire is at 2026-10-18 01:05 UTC" in outcome.detail
    assert sleeps == []


def test_a_host_unsettled_past_its_deadline_ends_deferred_with_its_last_success():
    # A 12:00 merge fires at 12:05 and 12:20, so with the 60s delay and 10 minutes of slack
    # the deadline is 12:31.
    fetch, _ = _prom(_series(sha="c" * 12, success=str(int(_at(9, 35)))))
    outcome, sleeps = _wait(_pi(), fetch, [_at(12, 0), _at(12, 30), _at(12, 32)])
    assert (outcome.verdict, outcome.rc) == ("deferred", 75)
    assert "not applied it by 2026-10-17 12:31 UTC" in outcome.detail
    assert "last update success was at 2026-10-17 09:35 UTC" in outcome.detail
    assert len(sleeps) == 2


def test_an_unreachable_prometheus_is_not_settled_and_keeps_waiting():
    down, _ = _prom(loopback=REFUSED, cluster=REFUSED)
    up, _ = _prom(_series())
    answers = iter([down, down] + [up] * 3)
    outcome, sleeps = _wait(
        _pi(),
        lambda url, timeout: next(answers)(url, timeout),
        [_at(12, 0), _at(12, 1)],
    )
    assert outcome is None and sleeps == [host_wait.POLL_S]


def test_an_unknown_schedule_waits_the_full_cap_rather_than_skipping():
    target = replace(_pi(), triggers=None)
    fetch, _ = _prom(_series(sha="c" * 12))
    outcome, sleeps = _wait(target, fetch, [_at(12, 0), _at(12, 44), _at(12, 46)])
    assert (outcome.verdict, outcome.rc) == ("deferred", 75)
    assert "by 2026-10-17 12:45 UTC" in outcome.detail
    assert len(sleeps) == 2
