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
from _helpers import ANSIBLE


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

# Enough of the docker CLI for pi-recovery-health: the `ps -q` liveness probe, and `start`.
# Running containers live in $STATE_FILE, one name per line; $UNSTARTABLE names those whose
# start fails, which is the 2026-08-29 autoheal case.
DOCKER_STUB = """\
#!/usr/bin/env bash
case "$1" in
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
    case ",${UNSTARTABLE}," in
      *",$2,"*) exit 1 ;;
    esac
    echo "$2" >> "$STATE_FILE"
    ;;
esac
exit 0
"""

# pi-sd-health reads the ext4 error counter from a sysfs path it builds itself; point it at
# a temp file by substituting the assignment's value.
SD_COUNTER = "/sys/fs/ext4/mmcblk0p2/errors_count"


def render(name, tmp_path, jinja_vars=None):
    """The real template, rendered, with only its absolute paths repointed at temp files."""
    template = TEMPLATES / f"{name}.sh.j2"
    body = (
        jinja2.Environment(undefined=jinja2.StrictUndefined)
        .from_string(template.read_text())
        .render(
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


def run(name, tmp_path, running=(), unstartable="", push_ok="1", counter=None):
    """Run a health cron; return (status, msg, still_running, health_log_lines)."""
    script, log = render(name, tmp_path)

    if counter is not None:
        counter_file = tmp_path / "errors_count"
        counter_file.write_text(counter)
        script.write_text(script.read_text().replace(SD_COUNTER, str(counter_file)))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(DOCKER_STUB)
    docker.chmod(0o755)

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
            "KUMA_PUSH_OUT": str(out),
        },
    )

    status, msg = out.read_text().splitlines()
    lines = log.read_text().splitlines() if log.exists() else []
    return status, msg, set(state.read_text().split()), lines
