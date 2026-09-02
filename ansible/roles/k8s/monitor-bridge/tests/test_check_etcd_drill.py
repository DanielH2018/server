"""The etcd restore drill's stamp reader.

The reader was deliberately held back until the drill had a cron, because a fail-closed
staleness check against a stamp nothing keeps fresh sits red forever and trains the operator to
ignore it. The cron landed 2026-08-28 (`k3s_etcd_restore_drill_cron`), so these are the guards
that came with the reader. Every one of them is a way the check could report GREEN while the
restore path is unproven, which is the only failure mode that matters here.
"""

import os
import time
from pathlib import Path

import pytest
import yaml

import bridge_config
import checks_service
import check

_REPO = Path(__file__).resolve().parents[5]


# --- the etcd restore drill's stamp reader ------------------------------------------------------
#
# The reader was deliberately held back until the drill had a cron, because a fail-closed staleness
# check against a stamp nothing keeps fresh sits red forever and trains the operator to ignore it.
# The cron landed 2026-08-28 (k3s_etcd_restore_drill_cron), so these are the guards that came with
# the reader. Every one of them is a way the check could report GREEN while the restore path is
# unproven, which is the only failure mode that matters here.


def _stamp(tmp_path, monkeypatch, body, mode=0o644, name="last-success-list-only"):
    p = tmp_path / name
    p.write_text(body)
    p.chmod(mode)
    monkeypatch.setattr(bridge_config, "ETCD_DRILL_STATE_DIR", str(tmp_path))
    return p


def _stamp_body(age_days, mode="list-only"):
    epoch = time.time() - age_days * 86400
    return "mode=%s\nsnapshot=x.zip\nutc=whenever\nepoch=%f\n" % (mode, epoch)


def test_etcd_drill_passes_on_a_recent_stamp(tmp_path, monkeypatch):
    _stamp(tmp_path, monkeypatch, _stamp_body(1))
    ok, msg = checks_service.check_etcd_restore_drill()
    assert ok is True
    assert "1.0 days ago" in msg


def test_etcd_drill_fails_when_it_has_never_run(tmp_path, monkeypatch):
    """The state most worth reporting, and the one `[[ -f $STAMP ]] && check_age` reports green."""
    monkeypatch.setattr(bridge_config, "ETCD_DRILL_STATE_DIR", str(tmp_path))
    ok, msg = checks_service.check_etcd_restore_drill()
    assert ok is False
    assert "has ever passed" in msg


def test_etcd_drill_fails_when_the_stamp_is_unreadable(tmp_path, monkeypatch):
    """An unreadable stamp is not hypothetical, and must report distinctly from an absent one.

    The first real run wrote 0640 root:root under UMASK 027 while this pod runs as uid 1000. An
    unreadable stamp and an absent one are otherwise indistinguishable, so they must report
    distinctly — they need different fixes.
    """
    if os.geteuid() == 0:
        pytest.skip("root ignores the mode bits this asserts")
    _stamp(tmp_path, monkeypatch, _stamp_body(1), mode=0o000)
    ok, msg = checks_service.check_etcd_restore_drill()
    assert ok is False
    assert "unreadable" in msg


def test_etcd_drill_fails_on_a_stale_stamp(tmp_path, monkeypatch):
    _stamp(tmp_path, monkeypatch, _stamp_body(9))
    ok, msg = checks_service.check_etcd_restore_drill()
    assert ok is False
    assert "9.0 days ago" in msg


def test_etcd_drill_fails_on_an_unparseable_stamp(tmp_path, monkeypatch):
    _stamp(tmp_path, monkeypatch, "mode=list-only\nsnapshot=x.zip\n")
    ok, msg = checks_service.check_etcd_restore_drill()
    assert ok is False
    assert "epoch" in msg


def test_etcd_drill_never_accepts_the_full_stamp_as_coverage(tmp_path, monkeypatch):
    """Only the list-only leg is scheduled.

    Accepting `last-success-full` would report the object-graph restore as proven when nothing on
    this host has ever proven it — the 'one tier hiding behind another tier's evidence' shape.
    """
    _stamp(tmp_path, monkeypatch, _stamp_body(1, mode="full"), name="last-success-full")
    ok, msg = checks_service.check_etcd_restore_drill()
    assert ok is False, "a full-mode stamp must not satisfy the list-only reader"
    assert "has ever passed" in msg


def test_etcd_drill_grace_is_derived_from_the_cron():
    """A grace period must come from the schedule it interacts with, never be picked round.

    The drill runs weekly (Monday 10:20). A window at or under the cadence flaps on every normal
    week; a window at twice it silently tolerates a whole missed run, which is the miss this
    check exists to catch — the 24h-grace-against-a-23h-gap failure of 2026-08-25, one cadence up.
    """
    defaults = yaml.safe_load(
        (
            Path(check.__file__).resolve().parents[3]
            / "setup"
            / "k3s"
            / "defaults"
            / "main.yml"
        ).read_text()
    )
    _minute, _hour, dom, month, dow = defaults["k3s_etcd_restore_drill_cron"].split()
    assert (dom, month) == ("*", "*") and dow != "*", (
        "this window is derived from a WEEKLY cadence; if the cron stops being weekly, "
        "ETCD_DRILL_MAX_AGE_S has to move with it"
    )
    cadence_s = 7 * 86400
    assert bridge_config.ETCD_DRILL_MAX_AGE_S > cadence_s, (
        "a window at or under the 7-day cadence flaps on every normal week"
    )
    assert bridge_config.ETCD_DRILL_MAX_AGE_S < 2 * cadence_s, (
        "a window of two cadences tolerates a fully missed run, which is exactly what this "
        "check is for"
    )


def test_etcd_drill_is_registered_and_can_actually_push():
    """Registration and the token must land together, and both now have.

    Asserting membership alone would pass for a check registered against a token nothing can
    set — which pushes to nowhere forever, present in the code and absent from the world. So
    this asserts the pair, in both directions: a later edit that drops either half fails here
    rather than quietly producing a monitor that cannot page.
    """
    names = {name for name, _, _ in check.CHECKS}
    env_secret = (
        Path(check.__file__).resolve().parents[1] / "templates" / "env-secret.yaml.j2"
    ).read_text()
    registered = "etcd_restore_drill" in names
    tokened = "KUMA_PUSH_ETCD_DRILL" in env_secret
    assert registered, "an unregistered check never runs; the reader would be dead code"
    assert registered == tokened, (
        "etcd_restore_drill's CHECKS entry and its KUMA_PUSH_ETCD_DRILL env-secret key move "
        "together — one without the other is either a check that cannot page or a token "
        "nothing reads"
    )
