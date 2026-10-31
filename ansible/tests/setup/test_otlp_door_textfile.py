"""claude-otel-health writes its OTLP-door verdict to the textfile collector beside the Kuma push.

M6 batch B6 shadows the `k3s Telemetry OTLP Door` tile with `OtlpDoorClosed` and
`OtlpDoorNotReporting` (observability's host-checks.yaml). The rendered script runs under bash
with the Kuma library stubbed to record the push, the real check library sourced from the tree,
and `timeout` stubbed on PATH to answer the TCP probe, so neither the runner's network nor its
clock decides a case. The push token comes from `$CREDENTIALS_DIRECTORY`, where the unit's
`LoadCredential=` puts it.
"""

from _helpers import ROLES
from _shell_render import render_shell_script
from lib.proc_testing import fake_bin, run, write_exec

TEMPLATE = ("components", "claude-code", "claude-otel-health.sh.j2")
KUMA_LIB = "/usr/lib/homelab/kuma-push-lib.sh"
CHECK_LIB = "/usr/lib/homelab/homelab-check-lib.sh"
REAL_CHECK_LIB = ROLES / "setup" / "initial_setup" / "files" / "homelab-check-lib.sh"
PROM = "claude-otel-health.prom"


def _run(tmp_path, door_open: bool, minute: str | None = None):
    """Run the rendered script once; return (the check file's series, the Kuma pushes)."""
    overrides = (
        None if minute is None else {"claude_code_telemetry_health_minute": minute}
    )
    body = render_shell_script(*TEMPLATE, overrides=overrides)
    for literal in (KUMA_LIB, CHECK_LIB):
        assert literal in body, f"claude-otel-health.sh.j2 no longer sources {literal}"

    kuma_lib = tmp_path / "kuma-push-lib.sh"
    kuma_lib.write_text(
        'kuma_push() { printf "%s %s\\n" "$1" "$3" >> "$KUMA_PUSH_OUT"; }\n'
    )
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "kuma_push_token").write_text("stub-token")
    pushed = tmp_path / "pushed"
    pushed.write_text("")
    textfile = tmp_path / "textfile"
    textfile.mkdir()
    script = write_exec(
        tmp_path / "claude-otel-health",
        body.replace(KUMA_LIB, str(kuma_lib)).replace(CHECK_LIB, str(REAL_CHECK_LIB)),
    )
    stubs = fake_bin(
        tmp_path / "bin",
        timeout=f"exit {0 if door_open else 1}\n",
        logger="exit 0\n",
    )
    env = {
        "TEXTFILE_DIR": str(textfile),
        "KUMA_PUSH_OUT": str(pushed),
        "CREDENTIALS_DIRECTORY": str(credentials),
        "PATH": "/usr/bin:/bin",
    }
    run(["bash", str(script)], env=env, stub_bin=stubs, check=True)
    lines = (textfile / PROM).read_text().splitlines()
    series = dict(line.rsplit(" ", 1) for line in lines if not line.startswith("#"))
    return series, [line.split() for line in pushed.read_text().splitlines()]


def test_an_open_door_writes_success_one_and_the_derived_deadline(tmp_path):
    series, pushes = _run(tmp_path, door_open=True)
    assert series['homelab_check_success{check="claude-otel-health"}'] == "1"
    # */10 renders two periods plus 300s.
    assert (
        series['homelab_check_deadline_seconds{check="claude-otel-health"}'] == "1500"
    )
    assert [status for status, _ in pushes] == ["up"]


def test_the_push_url_carries_the_credential_token(tmp_path):
    _, pushes = _run(tmp_path, door_open=True)
    ((_, url),) = pushes
    assert url.endswith("/api/push/stub-token"), url


def test_a_closed_door_writes_success_zero(tmp_path):
    series, pushes = _run(tmp_path, door_open=False)
    assert series['homelab_check_success{check="claude-otel-health"}'] == "0"
    assert [status for status, _ in pushes] == ["down"]
