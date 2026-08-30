"""Guards for the */10 health crons' boot grace (kuma-push-lib.sh `boot_grace_active`).

A cron that fires seconds after a boot evaluates a half-started cluster and sends a
healthchecks.io `/fail`, which alerts IMMEDIATELY — the check's own period and grace never get a
say. That is why the fix is a skipped run rather than a wider grace, and why it is guarded here:
the failure mode of a boot guard is silence, so nothing observes it working.

The 2026-08-30 restart is the worked example. daniel-box booted at 07:39:48, the */10 crons ran at
07:40:00, and the last pod reached Ready at 07:45:06 — 5m18s of boot-to-Ready against a 12-second
head start. `longhorn-backup-health` and `uptime-kuma-alive` both paged.
"""

import re
import subprocess

import yaml
from _helpers import ANSIBLE

LIB = ANSIBLE / "roles/setup/initial_setup/files/kuma-push-lib.sh"
K3S_DEFAULTS = yaml.safe_load(
    (ANSIBLE / "roles/setup/k3s/defaults/main.yml").read_text()
)

GRACE_S = K3S_DEFAULTS["k3s_health_cron_boot_grace_s"]

# The */10 crons the grace is derived against, and the daily ones it deliberately does not cover.
FREQUENT_SCRIPTS = ("longhorn-backup-health.sh.j2", "disk-health.sh.j2")
DAILY_SCRIPTS = ("manifest-prune-check.sh.j2", "etcd-snapshot-offbox.sh.j2")

# Worst boot-to-Ready measured on 2026-08-30: 07:39:48 boot -> 07:45:06 last pod Ready.
WORST_BOOT_TO_READY_S = 318


def _run_guard(uptime: str, grace: int) -> int:
    """Exit status of `boot_grace_active` with /proc/uptime stubbed to `uptime`.

    The function reads the clock through an unqualified `cut`, so a shell function of that name
    shadows the binary — which lets the real production code run against a controlled uptime
    instead of a copy of its logic.
    """
    script = f"""
    source {LIB}
    cut() {{ {uptime}; }}
    logger() {{ :; }}
    boot_grace_active {grace} test-tag
    """
    return subprocess.run(["bash", "-c", script], capture_output=True).returncode


def test_guard_skips_the_run_just_after_boot():
    # ACCEPT: 12s of uptime is the 2026-08-30 case — the cron must not run.
    assert _run_guard("echo 12", GRACE_S) == 0


def test_guard_lets_the_run_proceed_once_the_grace_has_passed():
    # REJECT: past the grace the check must run. Without this half, a guard that always skipped
    # would look identical from the passing side — and would silence these crons permanently.
    assert _run_guard(f"echo {GRACE_S + 1}", GRACE_S) != 0


def test_guard_fails_open_when_the_clock_is_unreadable():
    # A guard that cannot read /proc/uptime must run the check, not suppress it forever.
    assert _run_guard("return 1", GRACE_S) != 0
    assert _run_guard("echo ''", GRACE_S) != 0


def test_grace_is_shorter_than_the_cron_interval():
    # THE derivation. The dead-men's graces are sized for a single missed slot, which only holds
    # while the guard cannot span two of them — whatever minute of the hour the host boots on.
    for key in (
        "k3s_longhorn_backup_health_cron_minute",
        "k3s_disk_health_cron_minute",
    ):
        minute = K3S_DEFAULTS[key]
        step = int(re.fullmatch(r"\*/(\d+)", minute).group(1))
        assert GRACE_S < step * 60, (
            f"{key}={minute} is a {step * 60}s interval; a {GRACE_S}s grace can skip two slots"
        )


def test_grace_covers_the_worst_observed_startup():
    # The other bound: below this the cron runs against a cluster still coming up, which is the
    # bug. Stated as a floor so a future trim has to argue with the measurement.
    assert GRACE_S > WORST_BOOT_TO_READY_S


def test_the_frequent_crons_call_the_guard():
    for name in FREQUENT_SCRIPTS:
        text = (ANSIBLE / "roles/setup/k3s/templates" / name).read_text()
        assert "boot_grace_active {{ k3s_health_cron_boot_grace_s }}" in text, (
            f"{name} feeds a /fail dead-man but does not skip its first post-boot run"
        )


def test_a_guarded_cron_still_beats_its_kuma_tile():
    # The half that is easy to miss: these scripts feed a Kuma push tile as well as a
    # healthchecks.io dead-man, and the two have different tolerances. `k3s Longhorn Backup` and
    # `daniel-box Disk` run a 1200s window against a 600s cron, so they tolerate exactly ONE
    # missed push — the one a silent skip would consume, leaving the margin to cron jitter.
    #
    # So the guard pushes `up` with a skip message instead of exiting quietly. Asserted at the
    # call site rather than by rendering the tile, because what breaks this is someone moving the
    # guard back above PUSH_URL for tidiness.
    for name in FREQUENT_SCRIPTS:
        text = (ANSIBLE / "roles/setup/k3s/templates" / name).read_text()
        guard = text.index("boot_grace_active {{ k3s_health_cron_boot_grace_s }}")
        assert text.index('PUSH_URL="') < guard, (
            f"{name}: the boot guard runs before PUSH_URL is set, so it cannot beat"
        )
        assert 'kuma_push up "skipped — host still booting"' in text[guard:], (
            f"{name}: the boot guard skips the run without keeping the Kuma tile's heartbeat"
        )


def test_a_skipped_run_pings_no_healthchecks_slug():
    # The other direction, and the one that would be worse. Pinging a dead-man's success URL for
    # a run that did not happen holds the check green on no evidence — the failure the dead-man
    # exists to catch. Silence is correct here; the grace covers it.
    for name in FREQUENT_SCRIPTS:
        text = (ANSIBLE / "roles/setup/k3s/templates" / name).read_text()
        guard = text.index("if boot_grace_active")
        skip_block = text[guard : text.index("\nfi\n", guard)]
        assert "hc-ping" not in skip_block and "HC_" not in skip_block, (
            f"{name}: the skip path pings healthchecks.io for a run that did not happen"
        )


def test_the_daily_crons_do_not():
    # Deliberate, and the reject half of the wiring pair: for a daily cron a skipped slot is a
    # skipped DAY. Their 1-hour graces already tolerate a late run.
    for name in DAILY_SCRIPTS:
        text = (ANSIBLE / "roles/setup/k3s/templates" / name).read_text()
        assert "boot_grace_active" not in text, (
            f"{name} runs daily — a boot skip costs a whole day of coverage"
        )
