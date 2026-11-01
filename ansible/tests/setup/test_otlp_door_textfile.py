"""claude-otel-health records its OTLP-door verdict as a journal line and a textfile, and pushes nothing.

M6 batch B6a replaced the `k3s Telemetry OTLP Door` Kuma tile with `OtlpDoorClosed` and
`OtlpDoorNotReporting` (observability's host-checks.yaml), and its Cutover-2 dropped the Kuma push.
`hosts/lib/tests/test_claude_artifacts_node_timers.py` holds that the script pushes nothing.

The rendered script runs under bash with the real check library sourced from the tree, `logger`
stubbed to record the journal line, and `timeout` stubbed on PATH to answer the TCP probe, so
neither the runner's network nor its clock decides a case.
"""

from _helpers import ROLES
from _shell_render import render_shell_script
from lib.proc_testing import fake_bin, run, write_exec

TEMPLATE = ("components", "claude-code", "claude-otel-health.sh.j2")
CHECK_LIB = "/usr/lib/homelab/homelab-check-lib.sh"
REAL_CHECK_LIB = ROLES / "setup" / "initial_setup" / "files" / "homelab-check-lib.sh"
PROM = "claude-otel-health.prom"


def _run(tmp_path, door_open: bool, minute: str | None = None):
    """Run the rendered script once; return (the check file's series, the journal lines)."""
    overrides = (
        None if minute is None else {"claude_code_telemetry_health_minute": minute}
    )
    body = render_shell_script(*TEMPLATE, overrides=overrides)
    assert CHECK_LIB in body, f"claude-otel-health.sh.j2 no longer sources {CHECK_LIB}"

    journal = tmp_path / "journal"
    textfile = tmp_path / "textfile"
    textfile.mkdir()
    script = write_exec(
        tmp_path / "claude-otel-health", body.replace(CHECK_LIB, str(REAL_CHECK_LIB))
    )
    stubs = fake_bin(
        tmp_path / "bin",
        timeout=f"exit {0 if door_open else 1}\n",
        logger=(
            "#!/usr/bin/env bash\n"
            "shift 2  # -t <tag>\n"
            'printf \'%s\\n\' "$*" >> "$LOGGER_OUT"\n'
        ),
    )
    env = {
        "TEXTFILE_DIR": str(textfile),
        "LOGGER_OUT": str(journal),
        "PATH": "/usr/bin:/bin",
    }
    run(["bash", str(script)], env=env, stub_bin=stubs, check=True)
    lines = (textfile / PROM).read_text().splitlines()
    series = dict(line.rsplit(" ", 1) for line in lines if not line.startswith("#"))
    return series, journal.read_text().splitlines()


def test_an_open_door_writes_success_one_and_the_derived_deadline(tmp_path):
    series, journal = _run(tmp_path, door_open=True)
    assert series['homelab_check_success{check="claude-otel-health"}'] == "1"
    # */10 renders two periods plus 300s.
    assert (
        series['homelab_check_deadline_seconds{check="claude-otel-health"}'] == "1500"
    )
    assert [line.split()[0] for line in journal] == ["status=up"]
    assert "OTLP receiver reachable" in journal[0]


def test_a_closed_door_writes_success_zero(tmp_path):
    series, journal = _run(tmp_path, door_open=False)
    assert series['homelab_check_success{check="claude-otel-health"}'] == "0"
    assert [line.split()[0] for line in journal] == ["status=down"]
    assert "OTLP receiver unreachable" in journal[0]
