"""Guards on the Loki read-route witness — the host cron that watches a route no pod can reach.

loki-homelab's read route has one caller, `probe.py` running as a host process, and its guard is a
ClientIP set of node-owned addresses. Two properties keep the witness honest, and each fails in a
way that reads as success:

TWO HOSTS, TWO VERDICTS. Which address a host arrives as depends on where the traefik pod sits,
so the route can be dead from one host and healthy from the other. Each host writes its own
textfile, which Prometheus labels with the host's origin.

THE DEADLINE EXCEEDS THE PRODUCER'S PERIOD. A deadline shorter than its timer's period fires the
NotReporting rule with nothing wrong.

M6 batch B2's Cutover-2 removed the Kuma push, so a run's verdict is its textfile
and its exit code.

Run: uv run pytest ansible/tests/setup/test_loki_route_witness.py
"""

import os

from lib.proc_testing import fake_bin, path_with, run, write_exec
from _helpers import ALL_VARS, REPO
from _shell_render import render_shell_script, rendered_shell_text
from lib import yaml_fast

ALL_VARS_VALUES = yaml_fast.safe_load(ALL_VARS.read_text())
WITNESS = ("components", "loki-route-witness", "loki-read-route-health.sh.j2")

# The deadline the verdict's textfile carries, and the timer period it has to cover.
MONITOR_INTERVAL_S = 7200
HOURLY_S = 3600
# The script's check-library and uv paths, which the harness repoints.
CHECK_LIB_PATH = "/usr/lib/homelab/homelab-check-lib.sh"
UV_PATH = "/usr/bin/uv"
CHECK_LIB = (
    REPO / "hosts" / "components" / "check-lib" / "files" / "homelab-check-lib.sh"
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


def kuma_push_markers(body: str) -> list[str]:
    """The Kuma push markers a rendered witness still carries, or nothing."""
    return [marker for marker in KUMA_PUSH_MARKERS if marker in body]


def _render() -> str:
    return render_shell_script(
        *WITNESS,
        overrides={
            "domain": "example.test",
            "k3s_metallb_ingress_vip": "10.0.0.240",
            "sys_user": "ubuntu",
            "host_python_version": "3.12",
            "loki_route_witness_boot_grace_s": 0,
        },
    )


def _run_witness(tmp_path, route_rc: int) -> int:
    """Render the witness and run it with the reader stubbed to exit `route_rc`.

    Only absolute paths are repointed: the check library (the role's real one) and `uv`, whose
    stub answers the reader invocation with `route_rc` and prints a body for
    anything else. Returns the exit code. The run's textfile lands in `tmp_path / "textfile"`.
    """
    body = _render()
    for needle in (CHECK_LIB_PATH, UV_PATH):
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
    body = body.replace(CHECK_LIB_PATH, str(CHECK_LIB)).replace(UV_PATH, str(uv))
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


def test_a_down_verdict_writes_zero_and_exits_nonzero_so_the_timer_reruns_it(
    tmp_path,
):
    """The kuma-check timer's contract: exit 1 after a down verdict, and Restart=on-failure reruns."""
    assert _run_witness(tmp_path, route_rc=1) == 1
    series = _textfile_series(tmp_path)
    assert series['homelab_check_success{check="loki-read-route-health"}'] == "0"


def test_an_up_verdict_writes_one_with_the_deadline_the_rules_pin_and_exits_zero(
    tmp_path,
):
    assert _run_witness(tmp_path, route_rc=0) == 0
    check = 'check="loki-read-route-health"'
    series = _textfile_series(tmp_path)
    assert series[f"homelab_check_success{{{check}}}"] == "1"
    assert series[f"homelab_check_deadline_seconds{{{check}}}"] == str(
        MONITOR_INTERVAL_S
    )


def test_the_witness_does_not_push_to_kuma():
    # RED if the push, its library, its token or its env file returns: the tiles are gone, so a
    # push would be refused (`by=kuma`) every run.
    body = _render()
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
