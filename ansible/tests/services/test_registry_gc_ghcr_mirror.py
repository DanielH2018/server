"""registry-gc's host-side half of the ghcr-mirror prune (#5541).

Each function is sliced out of the RENDERED script and run with `k3s` stubbed as a shell
function that logs each call and answers from canned output.

  * `write_mirror_keep_set()` fails closed. The offline prune keeps only the N newest revisions
    plus this set, so a set written from a failed read would let rank alone delete images live
    pods run.
  * `scale_up_and_report()` journals the prune's lines before anything else, and deletes the
    mirror Job only when it has not finished, so a failure keeps the log its message names.
"""

import os
import shlex
from pathlib import Path

from _shell_render import rendered_shell_function
from lib.proc_testing import run

KEEP_SET = rendered_shell_function(
    "k8s", "registry", "registry-gc.sh.j2", "write_mirror_keep_set"
)
LIVE = "sha256:" + "a" * 64
PINNED = "sha256:" + "b" * 64


def _write(tmp_path: Path, get_rc: int, get_body: str) -> tuple[list[str], str]:
    """Run write_mirror_keep_set once; return the kubectl calls it made and MIRROR_NOTE."""
    calls = tmp_path / "calls"
    script = f"""
KUBECTL="k3s kubectl -n homelab"
MIRROR_KEEP_CM=registry-gc-ghcr-mirror-keep
MIRROR_NOTE=""
k3s() {{
  printf '%s\\n' "$*" >> {shlex.quote(str(calls))}
  if [[ "$*" == *" get "* ]]; then printf '%s' "$GET_BODY"; return "$GET_RC"; fi
}}
{KEEP_SET}
write_mirror_keep_set
printf '%s' "$MIRROR_NOTE"
"""
    env = {**os.environ, "GET_RC": str(get_rc), "GET_BODY": get_body}
    note = run(["bash", "-c", script], env=env, check=True).stdout
    return calls.read_text().splitlines(), note


def test_digests_from_statuses_and_specs_in_every_namespace_are_written(tmp_path):
    body = f'{{"items": [{{"imageID": "ghcr.io/x@{LIVE}"}}, {{"image": "ghcr.io/y:1@{PINNED}"}}]}}'
    calls, note = _write(tmp_path, 0, body)

    assert note == ""
    (get,) = [c for c in calls if " get " in c]
    assert " -A " in get and "cronjobs" in get, get
    (create,) = [c for c in calls if " create configmap " in c]
    assert f"--from-literal=digests={LIVE}\n{PINNED}" in "\n".join(calls), calls
    assert create.startswith(
        "kubectl -n homelab create configmap registry-gc-ghcr-mirror-keep"
    )


def test_an_unreadable_cluster_writes_no_keep_set(tmp_path):
    calls, note = _write(tmp_path, 1, "")

    assert any(" delete configmap registry-gc-ghcr-mirror-keep" in c for c in calls), (
        calls
    )
    assert not any(" create " in c for c in calls), calls
    assert "skipped" in note, note


def test_a_cluster_with_no_digest_writes_no_keep_set(tmp_path):
    calls, note = _write(tmp_path, 0, '{"items": []}')

    assert not any(" create " in c for c in calls), calls
    assert "skipped" in note, note


SCALE_UP = rendered_shell_function(
    "k8s", "registry", "registry-gc.sh.j2", "scale_up_and_report"
)
JOURNAL = rendered_shell_function(
    "k8s", "registry", "registry-gc.sh.j2", "journal_mirror_prune"
)
PRUNED_LINE = f"pruned danielh2018/nut@{LIVE} tags=none"


def _scale_up(tmp_path: Path, finished: str) -> list[str]:
    """Run scale_up_and_report after a run that stopped early; return every call in order."""
    calls = tmp_path / "calls"
    script = f"""
KUBECTL="k3s kubectl -n homelab"
DEPLOY=registry
MIRROR_JOB=registry-gc-ghcr-mirror
MIRROR_STARTED=1
MIRROR_DONE=0
k3s() {{
  printf '%s\\n' "$*" >> {shlex.quote(str(calls))}
  case "$*" in
    *" logs "*) printf 'ghcr-mirror prune: 1 revision(s)\\n%s\\n' "$PRUNED_LINE" ;;
    *" get job "*) printf '%s' "$FINISHED" ;;
  esac
}}
logger() {{ printf 'logger %s\\n' "$(cat)" >> {shlex.quote(str(calls))}; }}
finish() {{ :; }}
{JOURNAL}
{SCALE_UP}
scale_up_and_report
"""
    env = {**os.environ, "FINISHED": finished, "PRUNED_LINE": PRUNED_LINE}
    run(["bash", "-c", script], env=env, check=True)
    return calls.read_text().splitlines()


def _index(calls: list[str], needle: str) -> int:
    (i,) = [n for n, c in enumerate(calls) if needle in c]
    return i


def test_an_unfinished_mirror_job_is_journaled_then_deleted_before_scale_up(tmp_path):
    calls = _scale_up(tmp_path, finished="")

    assert f"logger {PRUNED_LINE}" in calls, calls
    journal = _index(calls, f"logger {PRUNED_LINE}")
    delete = _index(calls, "delete job registry-gc-ghcr-mirror")
    scale = _index(calls, "scale deploy/registry --replicas=1")
    assert journal < delete < scale, calls


def test_a_finished_mirror_job_is_journaled_and_kept_for_its_log(tmp_path):
    calls = _scale_up(tmp_path, finished="1")

    assert f"logger {PRUNED_LINE}" in calls, calls
    assert not any("delete job" in c for c in calls), calls
