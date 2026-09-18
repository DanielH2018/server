"""swallowed_verdicts: a host cron's DOWN verdict that kuma-push-lib.sh logged and lost (#1869).

Each fixture line is one rsyslog shipped, copied from the daniel-box journal, so the parser is
tested against what the emitters write rather than what their source suggests. The positive
input is the lone release-staleness-check http=500 of 2026-09-10 13:01; the negative inputs are
the daniel-box-down burst of 2026-09-09, where every push failed and nothing landed.
"""

import re
from pathlib import Path

import bridge.net
import checks.logs
import gates
import registry
from verdicts.logs import HC_ROUTED_TAGS, parse_push_line, swallowed_verdicts

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
# Kuma's own `Monitor not found or not active.` answer, as the library marks it since #1803.
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
    # 2026-09-09 18:31-19:01: daniel-box down, every cron on the estate lost its push. The
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
    # ACCEPT (#1803): the token the cron holds is not a live monitor. An `up` verdict, no
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
    # be dropped, or the tile pages twice / not at all for that cron.
    roles = Path(__file__).resolve().parents[3]
    readers = {
        p.name.removesuffix(".sh.j2")
        for p in roles.rglob("templates/*.sh.j2")
        if "KUMA_PUSH_OK" in p.read_text()
    }
    assert readers == HC_ROUTED_TAGS, sorted(readers ^ HC_ROUTED_TAGS)


def _patch_fetch(monkeypatch, fetch):
    """The file's one seam onto `bridge.net.loki_lines(cfg, logql, window_s, limit)`."""
    monkeypatch.setattr(bridge.net, "loki_lines", fetch)


def _streams(syslog, pod):
    """A fetch answering the syslog selector with `syslog` and the pod selector with `pod`."""

    def _lines(cfg, logql, window_s, limit):
        if logql == checks.logs.SWALLOWED_VERDICTS_LOGQL:
            return syslog
        if logql == checks.logs.SWALLOWED_VERDICTS_POD_LOGQL:
            return pod
        raise AssertionError("unexpected LogQL: %s" % logql)

    return _lines


def test_a_fetch_error_fails_open_and_names_the_owner(monkeypatch, cfg):
    # The Loki gate probes /labels, which stays fast while a range query is what a busy Loki
    # is slow at, so a raise here would page this tile for a slow Loki, not a lost verdict.
    def _raise(*a, **k):
        raise RuntimeError("loki-homelab: timed out")

    _patch_fetch(monkeypatch, _raise)
    ok, msg = checks.logs.check_swallowed_verdicts(cfg)
    assert ok
    assert "timed out" in msg and "Loki Reachable" in msg


# pi-peer-backup's CronJob container, the one pusher that is a pod rather than a host cron
# (#1943). Its script echoes the syslog shape, host = the pod name, so the reader sees it.
_POD_H = "2026-09-10T13:05:00Z pi-peer-backup-29312345-x7k2q pi-peer-backup: "
_POD_RUN_UP = _POD_H + "status=up pulled 2 peer file(s) from daniel-pi"
_POD_REJECTED = _POD_H + (
    "push failed (http=404 rc=0 by=kuma) (status=up: pulled 2 peer file(s) from daniel-pi)"
)
# The shape the CronJob wrote before #1943, which the reader must still not match: a line
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
    assert names == ["pull"], names
    assert '{container="pull"}' in checks.logs.SWALLOWED_VERDICTS_POD_LOGQL
    assert '!= "push failed transiently"' in checks.logs.SWALLOWED_VERDICTS_POD_LOGQL


def test_a_rejected_push_from_the_pod_stream_is_flagged(monkeypatch, cfg):
    # ACCEPT: the pod stream's lines reach the verdict alongside syslog's.
    _patch_fetch(
        monkeypatch,
        _streams([(1, _SIBLING_RUN)], [(2, _POD_RUN_UP), (3, _POD_REJECTED)]),
    )
    ok, msg = checks.logs.check_swallowed_verdicts(cfg)
    assert not ok
    assert (
        "Kuma rejected the push for pi-peer-backup on pi-peer-backup-29312345-x7k2q"
        in msg
    )


def test_the_pre_1943_pod_line_is_not_read_as_anything(monkeypatch, cfg):
    # REJECT (the pair): the old shape parses to nothing, so it neither pages nor counts as a
    # landed sibling.
    assert parse_push_line(_POD_PRE_1943) is None
    _patch_fetch(monkeypatch, _streams([(1, _SIBLING_RUN)], [(2, _POD_PRE_1943)]))
    ok, msg = checks.logs.check_swallowed_verdicts(cfg)
    assert ok, msg
    assert "1 tag(s) pushed" in msg


def test_a_capped_pod_fetch_reports_truncation_too(monkeypatch, cfg):
    cap = checks.logs.SWALLOWED_VERDICTS_LIMIT
    _patch_fetch(monkeypatch, _streams([(1, _SIBLING_RUN)], [(2, _POD_RUN_UP)] * cap))
    ok, msg = checks.logs.check_swallowed_verdicts(cfg)
    assert ok
    assert "hit its line cap" in msg


def test_a_failed_pod_fetch_keeps_the_syslog_verdict_and_says_so(monkeypatch, cfg):
    # The syslog arm caught setup-drift-check and release-staleness-check on its own; a slow
    # or failing pod query must not fail that arm open with it.
    def _fetch(cfg, logql, window_s, limit):
        if logql == checks.logs.SWALLOWED_VERDICTS_POD_LOGQL:
            raise RuntimeError("loki-homelab: pod query timed out")
        return [(1, _RUN_DOWN), (2, _SWALLOWED_DOWN), (3, _SIBLING_RUN)]

    _patch_fetch(monkeypatch, _fetch)
    ok, msg = checks.logs.check_swallowed_verdicts(cfg)
    assert not ok
    assert "release-staleness-check on daniel-box (http=500 rc=0)" in msg
    assert "pod-stream fetch unavailable: loki-homelab: pod query timed out" in msg
