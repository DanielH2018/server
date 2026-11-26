#!/usr/bin/env python3
"""Render a daniel-pi heartbeat script and run it under bash against stubs.

Used by test_pi_recovery_restarts_and_reports.py.
Not a test module itself.

These scripts are exercised for real rather than pattern-matched, because what breaks is
shell logic and line formatting, neither of which a textual guard can see. The render goes
through `_shell_render`, so what runs here cannot differ from what the shellcheck gate lints. Only two absolute paths are repointed at temp files -- the check library it sources,
and the health log it appends to. Every decision the script makes runs unmodified. The scripts
push nothing since M6 batch B5's Cutover-2, so the verdict a run reports is read back from the
health log line it appends.

The scripts are the `pi-heartbeats` host component's (H3.8b), and `COMPONENT_DEFAULTS` is its
`component.yml` `defaults:` map.
"""

import re

from _helpers import REPO, load_yaml
from lib.estate import host_vars
from lib.inventory_lists import PI_HOST
from _shell_render import render_shell_script
from lib.proc_testing import fake_bin, path_with, write_exec
from lib.proc_testing import run as launch
from validate.render_lib.shell import COMPONENTS


COMPONENT = COMPONENTS / "pi-heartbeats"
COMPONENT_DEFAULTS = load_yaml(COMPONENT / "component.yml")["defaults"]
# The scripts source the package path; the test sources the role's file, so it needs neither the
# package nor an installed library.
REAL_CHECK_LIB = "/usr/lib/homelab/homelab-check-lib.sh"
ROLE_CHECK_LIB = (
    REPO / "hosts" / "components" / "check-lib" / "files" / "homelab-check-lib.sh"
)
REAL_LOG = "/var/log/pi-health/health.log"
# Knobs: $UNSTARTABLE (start fails), $STUB_EXIT/$STUB_ERROR (stopped; proves inspect ran first).
# $STUB_DAEMON_DOWN dockerd gone, $STUB_GONE mid-recreate, $STUB_DAEMON_BACK daemon back.
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

# The real journalctl would read THIS host's journal, so the stub shadows it on PATH.
JOURNALCTL_STUB = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$JOURNALCTL_CALLS"
printf '%b' "$STUB_JOURNAL"
"""

# The real findmnt would read THIS host's mount table, where /var/log is no tmpfs; an empty value is
# no mount.
FINDMNT_STUB = """\
#!/usr/bin/env bash
[ -n "$STUB_LOG_FSTYPE" ] || exit 1
echo "$STUB_LOG_FSTYPE"
"""

SD_COUNTER = "/sys/fs/ext4/mmcblk0p2/errors_count"

GZ_STATE_VAR = "optimize_pi_gz_integrity_state_file"


PI_HOST_VARS = host_vars(PI_HOST)


def render(name, tmp_path, jinja_vars=None):
    """The real template, rendered through `_shell_render`, with its absolute paths repointed
    at temp files.

    `compose_services` is the Pi's own (hosts/daniel-pi/vars.yml) rather than a group default, so
    the accessor's inventory context does not carry it -- it goes in as an override. Everything
    else the templates read (domain, the ingress VIP, the gz-integrity defaults) comes from
    that same context, the one `validate/render_lib/shell.py` lints
    against. `jinja_vars` is laid on top, for a caller overriding any key by name -- the
    gz-integrity state-file path, most often.
    """
    overrides = {
        "compose_services": PI_HOST_VARS["compose_services"],
        **(jinja_vars or {}),
    }
    body = render_shell_script(
        COMPONENTS.name, COMPONENT.name, f"{name}.sh.j2", overrides=overrides
    )

    for literal, label in (
        (REAL_CHECK_LIB, "check library"),
        (REAL_LOG, "health log"),
    ):
        assert literal in body, (
            f"{name}.sh.j2 no longer references {literal} ({label}) -- this harness's "
            "substitution hook is gone, so it is not exercising the real script any more"
        )

    log = tmp_path / "health.log"

    body = body.replace(REAL_CHECK_LIB, str(ROLE_CHECK_LIB)).replace(REAL_LOG, str(log))

    return write_exec(tmp_path / f"{name}.sh", body), log


def run(
    name,
    tmp_path,
    running=(),
    unstartable="",
    counter=None,
    jinja_vars=None,
    exit_code="137",
    error="",
    daemon_down=False,
    daemon_back=False,
    gone="",
    journal="",
    log_fstype="tmpfs",
):
    """Run a heartbeat script; return (status, msg, still_running, health_log_lines).

    `status` and `msg` are the verdict the script appended to the health log, which is also what
    the textfile's success value follows.

    `log_fstype` is what pi-sd-health's findmnt probe reads for /var/log; `""` is no mount.
    `journal` is what the journalctl stub answers, `\\n`-separated; `journalctl_calls(tmp_path)`
    reads back every invocation the script made.

    pi-sd-health also reads the daily rotated-log integrity verdict, so an `up` one is seeded in
    tmp_path unless the caller points the script at its own file through `jinja_vars`. Without
    the seed every run here would read the real /var/lib path, find nothing, and push `down`.
    """
    jinja_vars = dict(jinja_vars or {})
    if GZ_STATE_VAR not in jinja_vars:
        verdict = tmp_path / "gz-integrity.state"
        verdict.write_text("up\nseeded by the test harness\n")
        jinja_vars[GZ_STATE_VAR] = str(verdict)

    script, log = render(name, tmp_path, jinja_vars)

    if counter is not None:
        counter_file = tmp_path / "errors_count"
        counter_file.write_text(counter)
        script.write_text(script.read_text().replace(SD_COUNTER, str(counter_file)))

    bin_dir = fake_bin(
        tmp_path / "bin",
        docker=DOCKER_STUB,
        journalctl=JOURNALCTL_STUB,
        findmnt=FINDMNT_STUB,
    )

    state = tmp_path / "running"
    state.write_text("".join(f"{c}\n" for c in running))
    textfiles = tmp_path / "textfile"
    textfiles.mkdir(exist_ok=True)

    launch(
        ["bash", str(script)],
        check=True,
        env={
            "PATH": path_with(bin_dir, env={"PATH": "/usr/bin:/bin"}),
            "STATE_FILE": str(state),
            "UNSTARTABLE": unstartable,
            "STUB_EXIT": exit_code,
            "STUB_ERROR": error,
            "STUB_DAEMON_DOWN": "1" if daemon_down else "0",
            "STUB_DAEMON_BACK": "1" if daemon_back else "0",
            "STUB_GONE": gone,
            "STUB_JOURNAL": journal,
            "STUB_LOG_FSTYPE": log_fstype,
            "JOURNALCTL_CALLS": str(tmp_path / "journalctl.calls"),
            "TEXTFILE_DIR": str(textfiles),
        },
    )

    lines = log.read_text().splitlines() if log.exists() else []
    verdict = re.search(r": status=(up|down) ?(.*)$", lines[-1])
    assert verdict, f"the health log's last line carries no verdict: {lines!r}"
    return verdict[1], verdict[2], set(state.read_text().split()), lines


def textfile(tmp_path, name) -> dict[str, float]:
    """The homelab_check_* series a run wrote for `name`, by metric name; {} when it wrote none."""
    path = tmp_path / "textfile" / f"{name}.prom"
    if not path.exists():
        return {}
    series = {}
    for line in path.read_text().splitlines():
        if line.startswith("homelab_check_"):
            metric, value = line.split(" ", 1)
            series[metric.split("{", 1)[0]] = float(value)
    return series


def journalctl_calls(tmp_path) -> list[str]:
    """Every argv the journalctl stub was invoked with, one string per call."""
    calls = tmp_path / "journalctl.calls"
    return calls.read_text().splitlines() if calls.exists() else []
