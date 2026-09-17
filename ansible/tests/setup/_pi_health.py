#!/usr/bin/env python3
"""Render a daniel-pi health cron and run it under bash against stubs.

Shared by test_pi_recovery_restarts_and_reports.py and test_pi_health_log_line_shape.py.
Not a test module itself.

These scripts are exercised for real rather than pattern-matched, because what breaks is
shell logic and line formatting, neither of which a textual guard can see. Only two
absolute paths are repointed at temp files -- the Kuma push helper it sources, and the
health log it appends to. Every decision the script makes runs unmodified.
"""

import subprocess
import jinja2
from _helpers import ANSIBLE, HOST_VARS, load_yaml


TEMPLATES = ANSIBLE / "roles" / "setup" / "optimize_pi" / "templates"
REAL_LIB = "/usr/local/lib/kuma-push-lib.sh"
REAL_LOG = "/var/log/pi-health/health.log"

# kuma_push, recording instead of pushing. Signature from
# roles/setup/initial_setup/files/kuma-push-lib.sh: STATUS MSG PUSH_URL HOST RESOLVE_IP TAG.
# KUMA_PUSH_OK mirrors the real lib's contract, so the push-failed branch is reachable.
LIB_STUB = """\
kuma_push() {
  KUMA_PUSH_OK="${STUB_PUSH_OK:-1}"
  printf '%s\\n%s\\n' "$1" "$2" > "$KUMA_PUSH_OUT"
}
"""

# Enough of the docker CLI for pi-recovery-health: the `ps -q` liveness probe, `inspect`,
# and `start`. Running containers live in $STATE_FILE, one name per line; $UNSTARTABLE names
# those whose start fails, which is the 2026-08-29 autoheal case. `inspect` answers with the
# real CLI's shape for whatever --format asks: a running container reads status=running, a
# stopped one carries $STUB_EXIT / $STUB_ERROR -- so a message that names an exit code proves
# the script inspected BEFORE it started the container, not after. $STUB_DAEMON_DOWN=1 is
# dockerd itself gone: every verb prints nothing and fails, which is what the real CLI does
# against a dead socket ("Cannot connect to the Docker daemon", on stderr). $STUB_GONE names
# a container the daemon no longer HAS -- mid-recreate under a deploy -- so `inspect` and
# `start` fail for it alone while the daemon answers everything else. $STUB_DAEMON_BACK=1
# on top of $STUB_DAEMON_DOWN is a daemon that died during the loop and answers `version`
# again by the time the script asks it directly.
DOCKER_STUB = """\
#!/usr/bin/env bash
if [ "${STUB_DAEMON_DOWN:-0}" = "1" ]; then
  if [ "$1" = version ] && [ "${STUB_DAEMON_BACK:-0}" = "1" ]; then
    echo 29.5.3
    exit 0
  fi
  echo "Cannot connect to the Docker daemon at unix:///var/run/docker.sock" >&2
  exit 1
fi
case "$1" in
  inspect)
    name="${@: -1}"
    if [ "$name" = "${STUB_GONE:-}" ]; then
      echo "Error: No such object: $name" >&2
      exit 1
    elif grep -qxF "$name" "$STATE_FILE" 2>/dev/null; then
      echo "status=running exit=0 finished=0001-01-01T00:00:00Z error="
    else
      printf 'status=exited exit=%s finished=2026-09-13T07:36:32.677805668Z error=%s\\n' \\
        "${STUB_EXIT:-137}" "${STUB_ERROR:-}"
    fi
    ;;
  ps)
    name=""
    for arg in "$@"; do
      case "$arg" in
        name=^*$) name="${arg#name=^}"; name="${name%$}" ;;
      esac
    done
    if grep -qxF "$name" "$STATE_FILE" 2>/dev/null; then
      echo "stubid_${name}"
    fi
    ;;
  start)
    case ",${UNSTARTABLE},${STUB_GONE:-}," in
      *",$2,"*) exit 1 ;;
    esac
    echo "$2" >> "$STATE_FILE"
    ;;
esac
exit 0
"""

# journalctl, answering `-u docker` with $STUB_JOURNAL and recording that it was asked. The
# real one is on /usr/bin and would read THIS host's journal, so the stub shadows it on PATH.
JOURNALCTL_STUB = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$JOURNALCTL_CALLS"
printf '%b' "$STUB_JOURNAL"
"""

# pi-sd-health reads the ext4 error counter from a sysfs path it builds itself; point it at
# a temp file by substituting the assignment's value.
SD_COUNTER = "/sys/fs/ext4/mmcblk0p2/errors_count"


# The Pi's own containers_list, so the recovery cron's watch set is the one the host deploys.
PI_HOST_VARS = load_yaml(HOST_VARS / "daniel-pi.yml")


def render(name, tmp_path, jinja_vars=None):
    """The real template, rendered, with only its absolute paths repointed at temp files."""
    template = TEMPLATES / f"{name}.sh.j2"
    body = (
        jinja2.Environment(undefined=jinja2.StrictUndefined)
        .from_string(template.read_text())
        .render(
            containers_list=PI_HOST_VARS["containers_list"],
            has_code_server=False,
            domain="example.test",
            k3s_metallb_ingress_vip="10.0.0.240",
            pi_recovery_push_token="stubtoken",
            pi_sd_health_push_token="stubtoken",
            **(jinja_vars or {}),
        )
    )

    for literal, label in ((REAL_LIB, "push helper"), (REAL_LOG, "health log")):
        assert literal in body, (
            f"{template.name} no longer references {literal} ({label}) -- this harness's "
            "substitution hook is gone, so it is not exercising the real script any more"
        )

    lib = tmp_path / "kuma-push-lib.sh"
    lib.write_text(LIB_STUB)
    log = tmp_path / "health.log"

    body = body.replace(REAL_LIB, str(lib)).replace(REAL_LOG, str(log))

    script = tmp_path / f"{name}.sh"
    script.write_text(body)
    script.chmod(0o755)
    return script, log


def run(
    name,
    tmp_path,
    running=(),
    unstartable="",
    push_ok="1",
    counter=None,
    jinja_vars=None,
    exit_code="137",
    error="",
    daemon_down=False,
    daemon_back=False,
    gone="",
    journal="",
):
    """Run a health cron; return (status, msg, still_running, health_log_lines).

    `journal` is what the journalctl stub answers, `\\n`-separated; `journalctl_calls(tmp_path)`
    reads back every invocation the script made.
    """
    script, log = render(name, tmp_path, jinja_vars)

    if counter is not None:
        counter_file = tmp_path / "errors_count"
        counter_file.write_text(counter)
        script.write_text(script.read_text().replace(SD_COUNTER, str(counter_file)))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(DOCKER_STUB)
    docker.chmod(0o755)
    journalctl = bin_dir / "journalctl"
    journalctl.write_text(JOURNALCTL_STUB)
    journalctl.chmod(0o755)

    state = tmp_path / "running"
    state.write_text("".join(f"{c}\n" for c in running))
    out = tmp_path / "push"

    subprocess.run(
        ["bash", str(script)],
        check=True,
        capture_output=True,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "STATE_FILE": str(state),
            "UNSTARTABLE": unstartable,
            "STUB_PUSH_OK": push_ok,
            "STUB_EXIT": exit_code,
            "STUB_ERROR": error,
            "STUB_DAEMON_DOWN": "1" if daemon_down else "0",
            "STUB_DAEMON_BACK": "1" if daemon_back else "0",
            "STUB_GONE": gone,
            "STUB_JOURNAL": journal,
            "JOURNALCTL_CALLS": str(tmp_path / "journalctl.calls"),
            "KUMA_PUSH_OUT": str(out),
        },
    )

    status, msg = out.read_text().splitlines()
    lines = log.read_text().splitlines() if log.exists() else []
    return status, msg, set(state.read_text().split()), lines


def journalctl_calls(tmp_path) -> list[str]:
    """Every argv the journalctl stub was invoked with, one string per call."""
    calls = tmp_path / "journalctl.calls"
    return calls.read_text().splitlines() if calls.exists() else []
