"""The backfill timer's unit and the harness it runs must agree about exit codes and flags.

The unit is a systemd template and the harness is a Python script, so nothing but a test keeps
them in step. Both failures here are silent in the direction that matters: a `SuccessExitStatus`
that no longer matches `CONDITION_NOT_MET` makes the unit page every hour for weeks — the
expected state while the streak is short — and an operator learns to ignore it before it ever
means anything. A missing flag makes the run a no-op that still exits 0.
"""

import re

import yaml
from _helpers import REPO

_REPO = REPO
_UNIT = (
    _REPO / "ansible/roles/setup/gitops_deploy/templates/staging-backfill.service.j2"
)
_TIMER = _REPO / "ansible/roles/setup/gitops_deploy/templates/staging-backfill.timer.j2"
_TASKS = _REPO / "ansible/roles/setup/gitops_deploy/tasks/main.yml"
_HARNESS = _REPO / "scripts/deploy_tools/backfill_staging_gate.py"


def onfailure_target(unit_text: str) -> str | None:
    """The unit named by `OnFailure=`, or None.

    A pure function so it can be given text that must be REJECTED — the real tree can only ever be
    observed passing.
    """
    match = re.search(r"^OnFailure=(\S+)$", unit_text, re.M)
    return match.group(1) if match else None


def installed_units(tasks_text: str) -> set[str]:
    """Every `*.service`/`*.timer` name the install loop enumerates. Pure for the same reason."""
    return set(re.findall(r"^\s*- (\S+\.(?:service|timer))\s*$", tasks_text, re.M))


def harness_constant(name: str) -> int:
    match = re.search(rf"^{name} = (\d+)$", _HARNESS.read_text(), re.M)
    assert match, f"{_HARNESS.name} no longer defines {name}"
    return int(match.group(1))


def test_the_units_tolerated_exit_is_the_harnesss_not_met_code():
    """NOT MET is the expected state for weeks, so it must not read as a failed unit."""
    tolerated = re.search(r"^SuccessExitStatus=(\d+)$", _UNIT.read_text(), re.M)
    assert tolerated, "the unit no longer tolerates any exit code"
    assert int(tolerated.group(1)) == harness_constant("CONDITION_NOT_MET")


def test_the_unit_does_not_tolerate_the_could_not_run_code():
    """The rejecting half, and the reason the harness splits three ways rather than two.

    A unit that also swallowed COULD_NOT_RUN would stay green through a harness that cannot
    run at all — the ratchet silently stopping, which looks identical to a quiet week.
    """
    tolerated = re.search(r"^SuccessExitStatus=(\d+)$", _UNIT.read_text(), re.M)
    assert int(tolerated.group(1)) != harness_constant("COULD_NOT_RUN")


def test_the_unit_passes_the_flags_the_scheduled_form_needs():
    # --since-ledger without --jsonl exits COULD_NOT_RUN, so the pair is the contract.
    exec_start = _UNIT.read_text()
    for flag in ("--since-ledger", "--jsonl", "--count", "--timeout"):
        assert flag in exec_start, f"the unit's ExecStart no longer passes {flag}"


def test_the_timer_follows_the_gates_own_switch():
    """A ratchet running while the gate is off deploys to staging for a measurement nobody is
    collecting — and the stop half is what keeps the switch a switch."""
    tasks = _TASKS.read_text()
    assert "staging-backfill.timer" in tasks
    assert "gitops_deploy_staging_gate" in tasks
    assert "stopped" in tasks, (
        "the timer is enabled but never stopped, so turning the gate off leaves the ratchet "
        "running"
    )


def test_the_timer_is_not_wanted_by_the_deploy_tick():
    # The ratchet is hourly and independent. Coupling it to the 30-minute deployer would put a
    # staging deploy inside the tick's budget, which is the thing slice 3 deliberately bounded.
    assert "gitops-deploy.service" not in _TIMER.read_text()


def test_the_unit_pages_on_the_failures_it_does_not_tolerate():
    """The other half of the exit-code split above.

    Preserving COULD_NOT_RUN as a failure is only worth anything if something observes the
    failed state. Nothing did until 2026-09-01: check.py reads no staging marker, no Kuma tile
    exists, node-exporter runs without --collector.systemd, and promtail ships pod logs rather
    than host journals. So the ratchet could stop dead and read exactly like a quiet week.
    """
    target = onfailure_target(_UNIT.read_text())
    assert target, (
        "the backfill unit pages on nothing; a failed ratchet is silent again"
    )
    assert target != "gitops-deploy-alert.service", (
        "that unit's payload names gitops-deploy and sends the operator to a journal that is "
        "healthy in exactly this case, so the page gets dismissed as a false alarm"
    )


def test_the_onfailure_target_exists_and_is_installed():
    """systemd does not validate an OnFailure= target at load, so a typo is silent.

    A misnamed target is indistinguishable from no target at all: the unit still fails, and
    systemd starts nothing.
    """
    target = onfailure_target(_UNIT.read_text())
    assert (_UNIT.parent / f"{target}.j2").is_file(), (
        f"{target} is named by OnFailure= but has no template"
    )
    assert target in installed_units(_TASKS.read_text()), (
        f"{target} has a template but the install loop never renders it"
    )


def test_a_typo_in_the_onfailure_target_is_flagged():
    """The rejecting half of the two tests above — the real tree can only be observed passing.

    Both helpers get input they must not accept: a target the install loop does not carry, and
    a unit with no OnFailure= at all.
    """
    typo = "staging-backfil-alert.service"
    assert onfailure_target(f"[Unit]\nOnFailure={typo}\n") == typo
    assert typo not in installed_units(_TASKS.read_text())
    assert onfailure_target("[Unit]\nDescription=no alerting here\n") is None


def heartbeat_writer(unit_text: str) -> str | None:
    """The path the unit's `ExecStopPost=` writes, or None. Pure, for the rejecting half."""
    match = re.search(r"^ExecStopPost=.*?> (.+?)'$", unit_text, re.M)
    return match.group(1) if match else None


def test_the_unit_writes_a_heartbeat_on_every_invocation():
    """`OnFailure=` covers "ran and failed"; nothing covered "is not running at all".

    ExecStopPost= runs whatever the outcome — the tolerated exit 1, a COULD_NOT_RUN 2, a
    TimeoutStartSec kill — which is the property this needs. ExecStartPost would fire only on
    the paths that reached the end successfully, leaving the heartbeat stale on exactly the runs
    that already page.
    """
    unit = _UNIT.read_text()
    assert heartbeat_writer(unit), (
        "the ratchet writes no heartbeat; a stopped timer is silent"
    )
    assert "ExecStartPost=" not in unit, (
        "ExecStartPost only fires when ExecStart succeeded, so it cannot answer 'did it run'"
    )


def test_the_heartbeat_path_is_the_one_the_reader_reads():
    """systemd validates no path here and the reader hardcodes a basename, so a rename on either
    side is silent: the check would report a ratchet that has never run, forever."""
    defaults = yaml.safe_load((_UNIT.parents[1] / "defaults" / "main.yml").read_text())
    written = heartbeat_writer(_UNIT.read_text())
    assert written == "{{ gitops_deploy_staging_backfill_heartbeat }}", (
        "the unit hardcodes a path instead of rendering the variable the reader is pinned to"
    )
    reader = (
        _REPO / "ansible/roles/k8s/monitor-bridge/files/checks_service.py"
    ).read_text()
    heartbeat = defaults["gitops_deploy_staging_backfill_heartbeat"]
    assert '"%s"' % heartbeat.rsplit("/", 1)[1] in reader


def test_the_heartbeat_writer_escapes_systemds_specifiers():
    """systemd expands `%` specifiers before /bin/sh sees the line, and `%s` is the USER SHELL.

    Unescaped, `date +%s` runs `date +/bin/bash`, the heartbeat holds `/bin/bash`, and the reader
    calls it unparseable — DOWN forever against a healthy ratchet, and green through every gate
    here, since the template text is what these tests read.
    """
    writer = re.search(r"^ExecStopPost=.*$", _UNIT.read_text(), re.M)
    assert writer, "the ratchet writes no heartbeat"
    unescaped = re.sub(r"%%", "", writer.group(0))
    assert "%" not in unescaped, (
        "a lone percent in a unit command line is a systemd specifier, not a literal — "
        "double it"
    )


def test_a_missing_heartbeat_writer_is_flagged():
    """The rejecting half — the real tree can only ever be observed passing."""
    assert heartbeat_writer("[Service]\nExecStart=/bin/true\n") is None


def test_the_armed_marker_moves_in_both_directions():
    """A disarmed ratchet is heartbeat-free by construction, so the reader needs to tell that
    from a broken one. A marker only ever created leaves the monitor permanently red on the
    disarm it exists to explain — the shape the timer task above already avoids."""
    tasks = _TASKS.read_text()
    assert "gitops_deploy_staging_backfill_armed_marker" in tasks
    assert (
        "state: \"{{ 'touch' if gitops_deploy_staging_gate else 'absent' }}\"" in tasks
    ), "the armed marker does not track the gate in both directions"


def test_the_alert_unit_names_the_backfill_rather_than_the_deployer():
    """The LAUNDER this fix exists to avoid, asserted rather than trusted.

    A sibling alert unit whose payload was copy-pasted from gitops-deploy-alert.service is the
    same wrong-unit page as reusing that unit outright, and reads as fixed.
    """
    alert = (_UNIT.parent / "staging-backfill-alert.service.j2").read_text()
    payload = re.search(r"^ExecStart=.*?(?=\n[A-Z#])", alert, re.M | re.S)
    assert payload, "the alert unit has no ExecStart"
    assert "staging-backfill" in payload.group(0)
    assert "journalctl -u gitops-deploy`" not in payload.group(0)


def test_the_alert_unit_reads_the_webhook_indirectly():
    """`systemctl show -p ExecStart` prints unit content to any local user with no sudo, so a
    0600 mode does not protect an embedded webhook — the sibling unit's own comment records
    that leak (2026-08-23b review M5)."""
    alert = (_UNIT.parent / "staging-backfill-alert.service.j2").read_text()
    assert "EnvironmentFile=/etc/gitops-deploy/alert-webhook" in alert
    assert "${ALERT_WEBHOOK}" in alert
    assert "https://discord" not in alert, (
        "the webhook is inlined; it leaks via systemctl show"
    )


def test_the_alert_units_delivery_line_survives_the_journald_cap():
    """journald stores notice+ on these hosts and `curl -fsS` is silent on success, so without
    BOTH directives a delivered page leaves `-- No entries --` — identical to never firing."""
    alert = (_UNIT.parent / "staging-backfill-alert.service.j2").read_text()
    assert "SyslogLevel=notice" in alert
    echo = re.search(r"^ExecStartPost=/bin/echo (.*)$", alert, re.M)
    assert echo, "a delivered page would leave no on-host record"
    assert "staging-backfill" in echo.group(1), (
        "the on-host record names the wrong unit, which is the payload trap one layer down"
    )
