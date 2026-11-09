"""registry-gc's report on a mirror's GC Job (#7918).

`report_mirror_job()` is sliced out of the RENDERED script and run with `k3s`, `logger` and
`sleep` stubbed as shell functions.

  * the wait ends on the Job's Failed condition as well as Complete. A failed Job never
    completes, so waiting on Complete alone held the mirror at zero for the whole deadline.
  * a failed Job's terminated containers and the gc container's log tail reach the journal,
    because the pod and its events go with the Job's TTL.
  * a skipped keep set reports the run down even though the store collected.
"""

import json
import os
import shlex
from pathlib import Path

from _shell_render import rendered_shell_function
from lib.proc_testing import run

REPORT = "\n".join(
    rendered_shell_function("components", "registry-gc", "registry-gc.sh.j2", name)
    for name in (
        "journal_mirror_prune",
        "wait_mirror_job",
        "journal_mirror_failure",
        "report_mirror_job",
    )
)
PRUNED_LINE = "pruned danielh2018/nut@sha256:" + "a" * 64 + " tags=none"
_VARS = """
KUBECTL="k3s kubectl -n homelab"
NS=homelab
MIRROR_DEPLOY=ghcr-mirror
MIRROR_JOB=registry-gc-ghcr-mirror
MIRROR_DONE=0
MIRROR_MSG=""
"""


def _report(
    tmp_path: Path, note: str = "", conditions: str = "Complete"
) -> tuple[int, str, list[str]]:
    """Run report_mirror_job against a mirror Job whose polls answer `conditions` in turn.

    `conditions` holds one line per poll of the Job's true conditions; the last repeats.
    Returns the rc, MIRROR_MSG, and every journal line and sleep, in order.
    """
    calls = tmp_path / "calls"
    polls = tmp_path / "polls"
    polls.write_text(conditions)
    script = f"""
{_VARS}MIRROR_NOTE="$NOTE"
k3s() {{
  case "$*" in
    *" get job "*conditions*)
      head -n1 {shlex.quote(str(polls))}
      [[ $(wc -l < {shlex.quote(str(polls))}) -gt 0 ]] && sed -i 1d {shlex.quote(str(polls))} ;;
    *" get pods "*) printf '%s' "$PODS" ;;
    *" logs "*" prune"*) printf '%s\\n' "$PRUNED_LINE" ;;
    *" logs "*" gc "*) printf 'level=info msg="marking manifest"\\n' ;;
  esac
}}
logger() {{ printf 'logger %s\\n' "$(cat)" >> {shlex.quote(str(calls))}; }}
sleep() {{ printf 'sleep %s\\n' "$1" >> {shlex.quote(str(calls))}; }}
{REPORT}
report_mirror_job
rc=$?
printf '%s\\n%s' "$rc" "$MIRROR_MSG"
"""
    env = {**os.environ, "NOTE": note, "PRUNED_LINE": PRUNED_LINE, "PODS": _FAILED_POD}
    rc, msg = run(["bash", "-c", script], env=env, check=True).stdout.split("\n", 1)
    return int(rc), msg, calls.read_text().splitlines() if calls.exists() else []


_FAILED_POD = json.dumps(
    {
        "items": [
            {
                "status": {
                    "initContainerStatuses": [
                        {
                            "name": "prune",
                            "state": {
                                "terminated": {"exitCode": 0, "reason": "Completed"}
                            },
                        }
                    ],
                    "containerStatuses": [
                        {
                            "name": "gc",
                            "state": {
                                "terminated": {"exitCode": 137, "reason": "OOMKilled"}
                            },
                        }
                    ],
                }
            }
        ]
    }
)


def test_a_completed_prune_keeps_the_run_up(tmp_path):
    rc, msg, _ = _report(tmp_path, "")

    assert rc == 0, msg
    assert "ghcr-mirror: 1 revision(s) pruned" in msg, msg


def test_a_skipped_prune_reports_the_run_down(tmp_path):
    """A persistent keep-set fault would otherwise skip every week behind a green tile."""
    note = "ghcr-mirror prune skipped, workloads unreadable"
    rc, msg, _ = _report(tmp_path, note)

    assert rc == 1, msg
    assert note in msg, msg


def test_a_failed_job_ends_the_wait_and_journals_its_exit(tmp_path):
    """#7918: a Job that died in 18s held the mirror down for the 1200s deadline."""
    rc, msg, calls = _report(tmp_path, conditions="FailureTarget Failed")

    assert rc == 1, msg
    assert (
        "job failed (prune exit=0 reason=Completed; gc exit=137 reason=OOMKilled)"
        in msg
    )
    assert not any(c.startswith("sleep") for c in calls), calls
    assert (
        "logger registry-gc-ghcr-mirror failed: prune exit=0 reason=Completed; "
        "gc exit=137 reason=OOMKilled" in calls
    ), calls
    assert (
        'logger registry-gc-ghcr-mirror gc: level=info msg="marking manifest"' in calls
    )


def test_a_running_job_is_polled_until_it_completes(tmp_path):
    rc, msg, calls = _report(tmp_path, conditions="\n\nComplete")

    assert rc == 0, msg
    assert [c for c in calls if c.startswith("sleep")] == ["sleep 10", "sleep 10"], (
        calls
    )
