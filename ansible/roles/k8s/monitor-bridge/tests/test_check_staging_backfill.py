"""The staging-gate backfill ratchet's run-recency reader.

`OnFailure=staging-backfill-alert.service` (PR #687) pages when a run FAILS. Nothing saw the
ratchet simply not running: a stopped timer produces no failures, so it is silent by
construction and reads exactly like a quiet week. This reader closes that, and every test here
is a way it could report GREEN while the ratchet is dead — plus the one way it could report RED
on a deliberate disarm, which is how a monitor gets ignored.
"""

import re
import time
from pathlib import Path

import bridge_config
import check
import checks_service
import yaml
from verdicts_service import staging_backfill_alive

_REPO = Path(__file__).resolve().parents[5]
_TIMER = _REPO / "ansible/roles/setup/gitops_deploy/templates/staging-backfill.timer.j2"
_UNIT = (
    _REPO / "ansible/roles/setup/gitops_deploy/templates/staging-backfill.service.j2"
)
_ROLE_DEFAULTS = _REPO / "ansible/roles/setup/gitops_deploy/defaults/main.yml"


def _state(tmp_path, monkeypatch, armed=True, age_s=None):
    if armed:
        (tmp_path / "staging-backfill-armed").write_text("")
    if age_s is not None:
        (tmp_path / "staging-backfill-last-run").write_text(
            "%d\n" % int(time.time() - age_s)
        )
    monkeypatch.setattr(bridge_config, "GITOPS_STATE_DIR", str(tmp_path))
    return tmp_path


# --- the pure verdict: one input it must accept, one it must reject -----------------------------


def test_a_fresh_heartbeat_is_accepted():
    ok, msg = staging_backfill_alive(True, 30 * 60, 150 * 60)
    assert ok is True
    assert "30m ago" in msg


def test_a_stale_heartbeat_is_rejected():
    """The rejecting half. A check is only ever observed passing, so without this there is no
    evidence it can go red at all."""
    ok, msg = staging_backfill_alive(True, 200 * 60, 150 * 60)
    assert ok is False
    assert "200m ago" in msg and "150m" in msg


def test_an_armed_ratchet_that_never_ran_is_rejected():
    """Distinct from stale, and the state most worth reporting: an age comparison alone cannot
    express it, so a reader that defaulted a missing heartbeat to age 0 would read green on a
    ratchet that has never run once."""
    ok, msg = staging_backfill_alive(True, None, 150 * 60)
    assert ok is False
    assert "never run" in msg


def test_a_disarmed_ratchet_is_up_and_says_why():
    """The disarm is deliberate — tasks/main.yml stops the timer when the gate is off — so a
    heartbeat-free ratchet is expected then. A monitor permanently red on a switch teaches an
    operator to ignore it, and one silently green says nothing about what it suppressed."""
    ok, msg = staging_backfill_alive(False, None, 150 * 60)
    assert ok is True
    assert "disarmed" in msg


# --- the reader over real files ----------------------------------------------------------------


def test_reader_passes_on_a_recent_heartbeat(tmp_path, monkeypatch):
    _state(tmp_path, monkeypatch, armed=True, age_s=45 * 60)
    ok, msg = checks_service.check_staging_backfill_alive()
    assert ok is True
    assert "45m ago" in msg


def test_reader_fails_on_a_stale_heartbeat(tmp_path, monkeypatch):
    _state(tmp_path, monkeypatch, armed=True, age_s=5 * 3600)
    ok, _msg = checks_service.check_staging_backfill_alive()
    assert ok is False


def test_reader_fails_closed_on_an_unparseable_heartbeat(tmp_path, monkeypatch):
    """A heartbeat written by something that is not the unit is not evidence the unit ran."""
    _state(tmp_path, monkeypatch, armed=True)
    (tmp_path / "staging-backfill-last-run").write_text("recently\n")
    ok, msg = checks_service.check_staging_backfill_alive()
    assert ok is False
    assert "unparseable" in msg


def test_reader_ignores_a_stale_heartbeat_once_disarmed(tmp_path, monkeypatch):
    """The marker is read FIRST. A disarm leaves the last heartbeat on disk, ageing forever."""
    _state(tmp_path, monkeypatch, armed=False, age_s=30 * 86400)
    ok, msg = checks_service.check_staging_backfill_alive()
    assert ok is True
    assert "disarmed" in msg


# --- wiring: the check, its token, its monitor and the paths it reads ---------------------------


def test_staging_backfill_is_registered_and_can_actually_push():
    """Registration and the token move together, in both directions.

    Membership alone would pass for a check registered against a token nothing can set — it
    pushes to nowhere forever, present in the code and absent from the world.
    """
    names = {name for name, _, _ in check.CHECKS}
    env_secret = (
        Path(check.__file__).resolve().parents[1] / "templates" / "env-secret.yaml.j2"
    ).read_text()
    registered = "staging_backfill" in names
    tokened = "KUMA_PUSH_STAGING_BACKFILL" in env_secret
    assert registered, "an unregistered check never runs; the reader would be dead code"
    assert registered == tokened, (
        "staging_backfill's CHECKS entry and its KUMA_PUSH_STAGING_BACKFILL env-secret key "
        "move together — one without the other is either a check that cannot page or a token "
        "nothing reads"
    )


def test_the_push_token_reaches_a_kuma_monitor():
    """A token in the pod's env with no monitor behind it is a push to a 404 forever — the
    half-wired shape this work was split out of #687 to avoid."""
    var = "monitor_bridge_staging_backfill_push_token"
    monitors = (
        _REPO / "ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2"
    ).read_text()
    env_secret = (
        Path(check.__file__).resolve().parents[1] / "templates" / "env-secret.yaml.j2"
    ).read_text()
    assert var in monitors, "no Kuma monitor carries this token"
    assert var in env_secret, "the bridge would push a token no monitor holds"
    registry = yaml.safe_load((_REPO / "ansible/secret_rotation.yml").read_text())
    assert var in registry["secrets"], (
        "an unregistered secret is excluded from rotation silently"
    )


def test_the_reader_and_the_unit_agree_on_the_file_names():
    """The reader hardcodes two basenames the Ansible side writes. systemd validates neither, so
    a rename on either side is a silent no-op: the check would report a ratchet that has never
    run, forever, against a ratchet running fine."""
    defaults = yaml.safe_load(_ROLE_DEFAULTS.read_text())
    heartbeat = defaults["gitops_deploy_staging_backfill_heartbeat"]
    marker = defaults["gitops_deploy_staging_backfill_armed_marker"]
    reader = (
        Path(checks_service.__file__).resolve().parent / "checks_service.py"
    ).read_text()
    assert heartbeat.endswith("/staging-backfill-last-run")
    assert marker.endswith("/staging-backfill-armed")
    assert '"staging-backfill-last-run"' in reader
    assert '"staging-backfill-armed"' in reader
    # Both live in the dir the pod already hostPath-mounts as GITOPS_STATE_DIR, which is what
    # lets this check need no mount of its own.
    assert heartbeat.startswith("/var/lib/gitops-deploy/")
    assert marker.startswith("/var/lib/gitops-deploy/")


def test_staging_backfill_window_is_derived_from_the_timer():
    """A grace period must come from the schedule it interacts with, never be picked round.

    OnUnitActiveSec=1h + RandomizedDelaySec=10min, with TimeoutStartSec=25min bounding the run
    itself, puts the worst-case gap between two heartbeats at about 95 minutes. A window at or
    under that flaps on an ordinary late run; a window at twice it tolerates a fully missed run,
    which is the miss this check exists to catch — the 24h-grace-against-a-23h-gap failure of
    2026-08-25, one cadence down.
    """
    timer = _TIMER.read_text()
    assert "OnUnitActiveSec=1h" in timer and "RandomizedDelaySec=10min" in timer, (
        "this window is derived from an hourly timer with 10 minutes of jitter; if the timer "
        "moves, STAGING_BACKFILL_MAX_AGE_S has to move with it"
    )
    assert "TimeoutStartSec=25min" in _UNIT.read_text()
    worst_gap_s = (60 + 10 + 25) * 60
    assert bridge_config.STAGING_BACKFILL_MAX_AGE_S > worst_gap_s, (
        "a window under the worst-case gap flaps on an ordinary late run"
    )
    assert bridge_config.STAGING_BACKFILL_MAX_AGE_S < 2 * worst_gap_s, (
        "a window of two cadences tolerates a fully missed run, which is what this check is for"
    )


def test_the_rendered_window_matches_the_code_default():
    """The env-secret renders the window so it is tunable without a code change. A rendered value
    that disagrees with the code default makes the test above measure a number nothing runs."""
    env_secret = (
        Path(check.__file__).resolve().parents[1] / "templates" / "env-secret.yaml.j2"
    ).read_text()
    rendered = re.search(
        r'^\s*STAGING_BACKFILL_MAX_AGE_MIN: "(\d+)"$', env_secret, re.M
    )
    assert rendered, "the window is no longer rendered, so it is no longer tunable"
    assert float(rendered.group(1)) * 60 == bridge_config.STAGING_BACKFILL_MAX_AGE_S
