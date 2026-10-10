"""swallowed_verdicts: a host cron's DOWN verdict that kuma-push-lib.sh logged and lost.

Each fixture line is one rsyslog shipped, copied from the daniel-box journal, so the parser is
tested against what the emitters write rather than what their source suggests. The positive
input is a lone release-staleness-check http=500; the negative inputs are a daniel-box-down
burst, where every push failed and nothing landed.
"""

import re
from pathlib import Path

import checks.logs
import gates
import registry
from _fake_sources import FakeSources
from _shell_render import rendered_shell_texts
from verdicts.logs import HC_ROUTED_TAGS, parse_push_line, swallowed_verdicts


def _unread_clock():
    """The node uptime a check sees when `/proc/uptime` cannot be read: no post-reboot grace.

    Left to its default, the check reads the uptime of the machine running the suite, and a CI
    runner booted a minute ago puts the call inside BOOT_SETTLE_S, where it returns the grace's
    `skipped` instead of the verdict under test.
    """
    return None


_H = "2026-09-10T13:00:00.000000+00:00 daniel-box "
_RUN_DOWN = _H + (
    "release-staleness-check: status=down homepage: changed since applied: "
    "ansible/roles/k8s/homepage/templates/config/custom.css.j2"
)
_SWALLOWED_DOWN = _H + (
    "release-staleness-check: push failed (http=500 rc=0) (status=down: homepage: changed "
    "since applied: ansible/roles/k8s/homepage/templates/config/custom.css.j2)"
)
_TRANSIENT = _H + (
    "release-staleness-check: push failed transiently (http=500 rc=0) (status=down: homepage: "
    "changed since applied: ansible/roles/k8s/homepage/templates/config/custom.css.j2), "
    "retrying in 30s"
)
_SIBLING_RUN = _H + "longhorn-backup-health: status=up 12 backups, newest 2h ago"
_SIBLING_SWALLOWED = _H + (
    "longhorn-backup-health: push failed (http=000 rc=7) (status=down: backups in Error state)"
)
# The pre-retry library and the Pi's health.log carry no http= pair.
_PI_SWALLOWED = (
    "2026-08-29T19:12:26+00:00 daniel-pi pi-recovery-health: push failed (status=down: "
    "autoheal exited)"
)
_SWALLOWED_UP = _H + (
    "release-staleness-check: push failed (http=404 rc=0) (status=up: 0 service(s) stale)"
)
# Kuma's own `Monitor not found or not active.` answer, as the library marks it.
_REJECTED_UP = _H + (
    "release-staleness-check: push failed (http=404 rc=0 by=kuma) (status=up: 0 service(s) "
    "stale)"
)


def test_parse_reads_the_three_line_shapes_and_rejects_the_transient_one():
    assert parse_push_line(_RUN_DOWN) == (
        "release-staleness-check",
        "daniel-box",
        "run",
        "down",
        "homepage: changed since applied: "
        "ansible/roles/k8s/homepage/templates/config/custom.css.j2",
    )
    assert parse_push_line(_SWALLOWED_DOWN) == (
        "release-staleness-check",
        "daniel-box",
        "swallowed",
        "down",
        "http=500 rc=0",
    )
    assert parse_push_line(_PI_SWALLOWED) == (
        "pi-recovery-health",
        "daniel-pi",
        "swallowed",
        "down",
        "no http code",
    )
    assert parse_push_line(_REJECTED_UP) == (
        "release-staleness-check",
        "daniel-box",
        "rejected",
        "up",
        "http=404 rc=0 by=kuma",
    )
    assert parse_push_line(_TRANSIENT) is None
    assert parse_push_line(_H + "sshd: Accepted publickey for ubuntu") is None


def test_a_lone_swallowed_down_beside_a_landed_sibling_is_flagged():
    ok, msg = swallowed_verdicts(
        [(1, _RUN_DOWN), (2, _SWALLOWED_DOWN), (3, _SIBLING_RUN)], "3h", truncated=False
    )
    assert not ok
    assert "release-staleness-check on daniel-box (http=500 rc=0)" in msg
    assert "1 sibling tag(s) landed" in msg


def test_a_swallowed_down_that_the_next_run_landed_is_clean():
    # The tag's newest line is a run with no failure after it: the verdict reached Kuma.
    later_run = _RUN_DOWN.replace("13:00:00", "13:30:00")
    ok, msg = swallowed_verdicts(
        [(1, _RUN_DOWN), (2, _SWALLOWED_DOWN), (3, _SIBLING_RUN), (4, later_run)],
        "3h",
        truncated=False,
    )
    assert ok, msg
    assert "2 tag(s) pushed" in msg


def test_a_fleet_wide_loss_where_nothing_lands_is_clean_and_names_the_owner():
    # daniel-box down, every cron on the estate loses its push. The
    # host and edge tiles page for that; a second page here is one root cause twice.
    ups = _SIBLING_SWALLOWED.replace("longhorn-backup-health", "ups-secondary-health")
    ok, msg = swallowed_verdicts(
        [(1, _RUN_DOWN), (2, _SWALLOWED_DOWN), (3, ups)], "3h", truncated=False
    )
    assert ok, msg
    assert "fleet-wide" in msg
    assert "ups-secondary-health" in msg and "release-staleness-check" in msg


def test_a_swallowed_up_is_not_a_lost_verdict():
    ok, msg = swallowed_verdicts(
        [(1, _SWALLOWED_UP), (2, _SIBLING_RUN)], "3h", truncated=False
    )
    assert ok, msg


def test_a_push_kuma_rejected_is_flagged_whatever_its_status_and_company():
    # ACCEPT: the token the cron holds is not a live monitor. An `up` verdict, no
    # sibling in the window — both of the conditions that keep a plain swallowed push quiet —
    # and it still pages, because nothing else can: Kuma answered, so the edge tiles are green,
    # and a token with no monitor has no tile to reach a deadline.
    ok, msg = swallowed_verdicts([(1, _REJECTED_UP)], "3h", truncated=False)
    assert not ok
    assert "Kuma rejected the push for release-staleness-check on daniel-box" in msg
    assert "http=404 rc=0 by=kuma, status=up" in msg


def test_a_rejected_push_that_the_next_run_landed_is_clean():
    # REJECT (the pair): the newest line decides here as everywhere — the tokens were
    # re-aligned and the tag's next push landed.
    later_run = _RUN_DOWN.replace("13:00:00", "13:30:00")
    ok, msg = swallowed_verdicts(
        [(1, _REJECTED_UP), (2, later_run)], "3h", truncated=False
    )
    assert ok, msg


def test_the_newest_line_decides_regardless_of_input_order():
    # Loki returns streams, not one merged list; the caller sorts, but the verdict must not
    # depend on it.
    ok, _ = swallowed_verdicts(
        [(3, _SIBLING_RUN), (2, _SWALLOWED_DOWN), (1, _RUN_DOWN)], "3h", truncated=False
    )
    assert not ok


def test_a_capped_fetch_says_so_rather_than_reading_clean():
    ok, msg = swallowed_verdicts([(1, _SIBLING_RUN)], "3h", truncated=True)
    assert ok
    assert "line cap" in msg


def test_the_logql_excludes_the_transient_retry_line_at_the_source():
    # The transient line is emitted twice per lost push, so counting it toward the fetch cap
    # would triple the population the cap was sized against.
    assert '!= "push failed transiently"' in checks.logs.SWALLOWED_VERDICTS_LOGQL
    assert '{job="syslog"}' in checks.logs.SWALLOWED_VERDICTS_LOGQL


def test_the_check_is_registered_and_loki_gated():
    names = {c.name for c in registry.build_checks()}
    assert "swallowed_verdicts" in names
    assert "swallowed_verdicts" in gates.LOKI_DEPENDENT


def test_a_tag_that_pages_its_own_lost_push_through_healthchecks_is_not_counted():
    # longhorn-backup-health reads KUMA_PUSH_OK and sends hc-ping `/fail`, which alerts at
    # once; a second page here is one root cause twice. Its run line still lands a sibling.
    ok, msg = swallowed_verdicts(
        [
            (1, _SIBLING_SWALLOWED),
            (2, _RUN_DOWN.replace("release-staleness-check", "disk-health")),
        ],
        "3h",
        truncated=False,
    )
    assert ok, msg


def test_hc_routed_tags_are_exactly_the_scripts_that_read_kuma_push_ok():
    # Derived from the tree, not trusted from the constant: a fourth script that starts
    # reading KUMA_PUSH_OK to route `/fail` has to be listed here, and one that stops has to
    # be dropped, or the tile pages twice / not at all for that cron. Read from the RENDER,
    # not the source: a value that moves into a role default leaves a source-text match
    # reading `{{ ... }}`, which passes on nothing (#3178). The library itself is a shell
    # template since #4219, and it sets the variable rather than reading it.
    readers = {
        name.removesuffix(".sh.j2")
        for _plane, _role, name, text in rendered_shell_texts()
        if "KUMA_PUSH_OK" in text and name != "kuma-push-lib.sh.j2"
    }
    assert readers == HC_ROUTED_TAGS, sorted(readers ^ HC_ROUTED_TAGS)


def _src(fetch):
    """The file's one seam onto `Sources.loki_lines(logql, window_s, limit)`."""
    return FakeSources(loki_lines=fetch)


def _streams(syslog, pod):
    """A fetch answering the syslog selector with `syslog` and the pod selector with `pod`."""

    def _lines(logql, window_s, limit):
        if logql == checks.logs.SWALLOWED_VERDICTS_LOGQL:
            return syslog
        if logql == checks.logs.SWALLOWED_VERDICTS_POD_LOGQL:
            return pod
        raise AssertionError("unexpected LogQL: %s" % logql)

    return _lines


def test_a_fetch_error_fails_open_and_names_the_owner(cfg):
    # The Loki gate probes /labels, which stays fast while a range query is what a busy Loki
    # is slow at, so a raise here would page this tile for a slow Loki, not a lost verdict.
    def _raise(*a, **k):
        raise RuntimeError("loki-homelab: timed out")

    ok, msg = checks.logs.check_swallowed_verdicts(
        cfg, _src(_raise), uptime_s=_unread_clock
    )
    assert ok
    assert "timed out" in msg and "Loki Reachable" in msg


# pi-peer-backup's CronJob container, the one pusher that is a pod rather than a host cron
# Its script echoes the syslog shape, host = the pod name, so the reader sees it.
_POD_H = "2026-09-10T13:05:00Z pi-peer-backup-29312345-x7k2q pi-peer-backup: "
_POD_RUN_UP = _POD_H + "status=up pulled 2 peer file(s) from daniel-pi"
_POD_REJECTED = _POD_H + (
    "push failed (http=404 rc=0 by=kuma) (status=up: pulled 2 peer file(s) from daniel-pi)"
)
# The CronJob's earlier shape, which the reader must still not match: a line
# it silently read as a verdict would be a second way to be green while blind.
_POD_PRE_1943 = _POD_H + "kuma push failed (up: pulled 2 peer file(s) from daniel-pi)"

_CRONJOB = (
    Path(__file__).resolve().parents[2]
    / "pi-peer-backup"
    / "templates"
    / "cronjob.yaml.j2"
)


def test_the_pod_logql_names_the_pi_peer_backup_container():
    # The container name is the only thing tying the second selector to that CronJob, so it is
    # read from the template rather than trusted: a rename there would leave the selector
    # matching nothing and the check permanently green for this pusher.
    names = re.findall(r"^\s+- name: (\S+)\n\s+image:", _CRONJOB.read_text(), re.M)
    assert names == ["pi-peer-backup"], names
    assert '{container="pi-peer-backup"}' in checks.logs.SWALLOWED_VERDICTS_POD_LOGQL
    assert '!= "push failed transiently"' in checks.logs.SWALLOWED_VERDICTS_POD_LOGQL


def test_a_rejected_push_from_the_pod_stream_is_flagged(cfg):
    # ACCEPT: the pod stream's lines reach the verdict alongside syslog's.
    src = _src(_streams([(1, _SIBLING_RUN)], [(2, _POD_RUN_UP), (3, _POD_REJECTED)]))
    ok, msg = checks.logs.check_swallowed_verdicts(cfg, src, uptime_s=_unread_clock)
    assert not ok
    assert (
        "Kuma rejected the push for pi-peer-backup on pi-peer-backup-29312345-x7k2q"
        in msg
    )


def test_the_pre_1943_pod_line_is_not_read_as_anything(cfg):
    # REJECT (the pair): the old shape parses to nothing, so it neither pages nor counts as a
    # landed sibling.
    assert parse_push_line(_POD_PRE_1943) is None
    src = _src(_streams([(1, _SIBLING_RUN)], [(2, _POD_PRE_1943)]))
    ok, msg = checks.logs.check_swallowed_verdicts(cfg, src, uptime_s=_unread_clock)
    assert ok, msg
    assert "1 tag(s) pushed" in msg


def test_a_capped_pod_fetch_reports_truncation_too(cfg):
    cap = checks.logs.SWALLOWED_VERDICTS_LIMIT
    src = _src(_streams([(1, _SIBLING_RUN)], [(2, _POD_RUN_UP)] * cap))
    ok, msg = checks.logs.check_swallowed_verdicts(cfg, src, uptime_s=_unread_clock)
    assert ok
    assert "hit its line cap" in msg


def test_a_failed_pod_fetch_keeps_the_syslog_verdict_and_says_so(cfg):
    # The syslog arm caught setup-drift-check and release-staleness-check on its own; a slow
    # or failing pod query must not fail that arm open with it.
    def _fetch(logql, window_s, limit):
        if logql == checks.logs.SWALLOWED_VERDICTS_POD_LOGQL:
            raise RuntimeError("loki-homelab: pod query timed out")
        return [(1, _RUN_DOWN), (2, _SWALLOWED_DOWN), (3, _SIBLING_RUN)]

    ok, msg = checks.logs.check_swallowed_verdicts(
        cfg, _src(_fetch), uptime_s=_unread_clock
    )
    assert not ok
    assert "release-staleness-check on daniel-box (http=500 rc=0)" in msg
    assert "pod-stream fetch unavailable: loki-homelab: pod query timed out" in msg
