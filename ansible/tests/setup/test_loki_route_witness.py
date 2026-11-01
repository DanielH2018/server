"""Guards on the Loki read-route witness — the host cron that watches a route no pod can reach.

loki-homelab's read route has one caller, `probe.py` running as a host process, and its guard is a
ClientIP set of node-owned addresses. Three properties keep the witness honest, and each fails in a
way that reads as success:

TWO HOSTS, TWO VERDICTS. Which address a host arrives as depends on where the traefik pod sits,
so the route can be dead from one host and healthy from the other. Each host writes its own
textfile, which Prometheus labels with the host's origin.

THE DEADLINE EXCEEDS THE PRODUCER'S PERIOD. A deadline shorter than its timer's period fires the
NotReporting rule with nothing wrong.

THERE IS A WAY OUT. A host dropped from `loki_route_witness_hosts` must lose the timer, or it keeps
writing a verdict nothing expects from it.

M6 batch B2's Cutover-2 removed the Kuma push from both copies, so a run's verdict is its textfile
and its exit code.

Run: uv run pytest ansible/tests/setup/test_loki_route_witness.py
"""

import os

import pytest
from lib.proc_testing import fake_bin, path_with, run, write_exec
from _helpers import ALL_VARS, ANSIBLE
from _shell_render import render_shell_script, rendered_shell_text
from lib import yaml_fast

ALL_VARS_VALUES = yaml_fast.safe_load(ALL_VARS.read_text())
CRONS = (
    ANSIBLE / "roles" / "setup" / "initial_setup" / "tasks" / "crons.yml"
).read_text()
CRON_TASKS = yaml_fast.safe_load(CRONS)
WITNESS = ("setup", "initial_setup", "loki-read-route-health.sh.j2")
PACKAGED = ("components", "loki-route-witness", "loki-read-route-health.sh.j2")

CRON_NAME = "Loki read-route witness"
# The deadline the verdict's textfile carries, and the timer period it has to cover.
MONITOR_INTERVAL_S = 7200
HOURLY_S = 3600
# Each copy's check-library path, which the harness repoints at the role's real library.
CHECK_LIBS = {
    WITNESS: "/usr/local/lib/homelab-check-lib.sh",
    PACKAGED: "/usr/lib/homelab/homelab-check-lib.sh",
}
CHECK_LIB = (
    ANSIBLE / "roles" / "setup" / "initial_setup" / "files" / "homelab-check-lib.sh"
)
# What a render carries while it still pushes to Kuma: the library, its call, a push URL, or the
# credential and env file that fed the token.
KUMA_PUSH_MARKERS = (
    "kuma-push-lib",
    "kuma_push",
    "/api/push/",
    "PUSH_TOKEN",
    "loki-route-kuma-push.env",
)


def _timer_import() -> dict:
    """The import of kuma_check_timer.yml that schedules the witness; its vars are the schedule."""
    for task in _flatten(CRON_TASKS):
        target = task.get("ansible.builtin.import_tasks") or ""
        if (
            target.endswith("common/tasks/kuma_check_timer.yml")
            and (task.get("vars") or {}).get("kuma_check_cron_name") == CRON_NAME
        ):
            return task
    raise AssertionError(
        f"no kuma_check_timer.yml import replaces the `{CRON_NAME}` cron"
    )


def _flatten(tasks):
    for task in tasks or []:
        yield task
        yield from _flatten(task.get("block"))


def kuma_push_markers(body: str) -> list[str]:
    """The Kuma push markers a rendered witness still carries, or nothing."""
    return [marker for marker in KUMA_PUSH_MARKERS if marker in body]


def test_both_cluster_nodes_witness_and_nothing_else_does():
    """Non-vacuity, and the reason there are two: one host cannot see a route that is dead only from the other."""
    assert ALL_VARS_VALUES["loki_route_witness_hosts"] == [
        "daniel-box",
        "daniel-server",
    ], (
        "both prod cluster nodes witness the route from different source addresses; "
        "daniel-pi is not in its ClientIP set and daniel-stage is a different cluster"
    )


def test_the_deadline_exceeds_the_timer_period():
    """A deadline shorter than the producer's period fires NotReporting with nothing wrong."""
    on_calendar = _timer_import()["vars"]["kuma_check_on_calendar"]
    assert on_calendar.startswith("*-*-* *:"), (
        "this test reads the timer as hourly; give it a period if that changes"
    )
    assert MONITOR_INTERVAL_S >= 2 * HOURLY_S


def test_a_host_dropped_from_the_list_loses_the_timer_and_the_cron():
    """The way out. Without it a retired witness keeps writing a verdict nothing expects."""
    variables = _timer_import()["vars"]
    assert "loki_route_witness_hosts" in variables["kuma_check_state"], (
        "kuma_check_state must follow the witness list, or a dropped host keeps the timer"
    )
    assert variables["kuma_check_cron_name"] == CRON_NAME, (
        "the import must name the cron it replaces, or a host runs both"
    )


def _render(copy) -> str:
    return render_shell_script(
        *copy,
        overrides={
            "domain": "example.test",
            "k3s_metallb_ingress_vip": "10.0.0.240",
            "sys_user": "ubuntu",
            "host_python_version": "3.12",
            "loki_route_witness_boot_grace_s": 0,
        },
    )


def _run_witness(tmp_path, route_rc: int, copy=WITNESS) -> int:
    """Render one copy of the witness and run it with the reader stubbed to exit `route_rc`.

    Only absolute paths are repointed: the check library (the role's real one) and the two `uv`
    binaries, whose stub answers the reader invocation with `route_rc` and prints a body for
    anything else. Returns the exit code. The run's textfile lands in `tmp_path / "textfile"`.
    """
    body = _render(copy)
    check_lib = CHECK_LIBS[copy]
    for needle in (check_lib, "/home/ubuntu/.local/bin/uv", "/usr/local/bin/uv"):
        assert needle in body, (
            f"{needle} is no longer in the witness; this harness repoints it"
        )
    uv = write_exec(
        tmp_path / "uv",
        "#!/usr/bin/env bash\n"
        'case "$*" in *loki_route_health.py*) echo verdict; exit "$STUB_ROUTE_RC" ;;'
        " *) echo body ;; esac\n",
    )
    binstub = fake_bin(tmp_path / "bin", logger="#!/bin/sh\nexit 0\n")
    body = (
        body.replace(check_lib, str(CHECK_LIB))
        .replace("/home/ubuntu/.local/bin/uv", str(uv))
        .replace("/usr/local/bin/uv", str(uv))
    )
    script = write_exec(tmp_path / "loki-read-route-health.sh", body)
    (tmp_path / "textfile").mkdir()
    result = run(
        [str(script)],
        env={
            **os.environ,
            "PATH": path_with(binstub),
            "STUB_ROUTE_RC": str(route_rc),
            "TEXTFILE_DIR": str(tmp_path / "textfile"),
        },
        check=False,
    )
    return result.returncode


def _textfile_series(tmp_path) -> dict[str, str]:
    prom = tmp_path / "textfile" / "loki-read-route-health.prom"
    lines = prom.read_text().splitlines()
    return dict(line.rsplit(" ", 1) for line in lines if not line.startswith("#"))


@pytest.mark.parametrize("copy", [WITNESS, PACKAGED], ids=["role", "packaged"])
def test_a_down_verdict_writes_zero_and_exits_nonzero_so_the_timer_reruns_it(
    tmp_path, copy
):
    """The kuma-check timer's contract: exit 1 after a down verdict, and Restart=on-failure reruns."""
    assert _run_witness(tmp_path, route_rc=1, copy=copy) == 1
    series = _textfile_series(tmp_path)
    assert series['homelab_check_success{check="loki-read-route-health"}'] == "0"


@pytest.mark.parametrize("copy", [WITNESS, PACKAGED], ids=["role", "packaged"])
def test_an_up_verdict_writes_one_with_the_deadline_the_rules_pin_and_exits_zero(
    tmp_path, copy
):
    assert _run_witness(tmp_path, route_rc=0, copy=copy) == 0
    check = 'check="loki-read-route-health"'
    series = _textfile_series(tmp_path)
    assert series[f"homelab_check_success{{{check}}}"] == "1"
    assert series[f"homelab_check_deadline_seconds{{{check}}}"] == str(
        MONITOR_INTERVAL_S
    )


@pytest.mark.parametrize("copy", [WITNESS, PACKAGED], ids=["role", "packaged"])
def test_neither_copy_pushes_to_kuma(copy):
    # RED if the push, its library, its token or its env file returns: the tiles are gone, so a
    # push would be refused (`by=kuma`) every run.
    body = _render(copy)
    # Non-vacuity: the render is the real script, which still records its verdict.
    assert "write_check_textfile loki-read-route-health" in body
    assert kuma_push_markers(body) == []


def test_a_witness_that_pushes_to_kuma_is_flagged():
    pushing = (
        "source /usr/local/lib/kuma-push-lib.sh\n"
        ". /etc/homelab/loki-route-kuma-push.env\n"
        'PUSH_URL="https://kuma/api/push/${LOKI_ROUTE_WITNESS_PUSH_TOKEN}"\n'
        'kuma_push "$STATUS" "$MSG" "$PUSH_URL"\n'
    )
    assert kuma_push_markers(pushing) == list(KUMA_PUSH_MARKERS)


def test_the_verdict_reads_the_body_rather_than_the_exit_code():
    """probe.py's curl has no `-f`, so `404 page not found` arrives with exit 0."""
    assert "loki_route_health.py" in rendered_shell_text(*WITNESS), (
        "the witness must decide on the response body — an exit-code verdict reads UP through "
        "exactly the outage this cron exists to catch"
    )


def test_the_boot_grace_is_shorter_than_the_cron_period():
    """The library's contract: at most one slot may ever be skipped."""
    assert ALL_VARS_VALUES["loki_route_witness_boot_grace_s"] < HOURLY_S


# --- The packaged copy (H7.36) ----------------------------------------------------------------
# daniel-box's witness is the `loki-route-witness` component since H7.36. daniel-server keeps the
# Ansible-era script above until its package moves it, so both copies exist, and the reader's
# code is the same.

PACKAGED_READER = (
    ANSIBLE.parent
    / "hosts"
    / "components"
    / "loki-route-witness"
    / "loki_route_health.py"
)
ROLE_READER = (
    ANSIBLE / "roles" / "setup" / "initial_setup" / "files" / "loki_route_health.py"
)


def test_the_packaged_reader_runs_the_same_code_as_the_roles_reader():
    """Until the daniel-server PR deletes the role's copy, a fix to one belongs in both. The two
    differ only in the docstring paragraph that says where the file is installed."""
    marker = "\nimport json\n"
    packaged = PACKAGED_READER.read_text().split(marker, 1)[1]
    assert packaged == ROLE_READER.read_text().split(marker, 1)[1], (
        "loki_route_health.py drifted between hosts/components/loki-route-witness/ and the "
        "initial_setup role"
    )
