"""registry-gc's host-side half of the ghcr-mirror prune (#5541).

Each function is sliced out of the RENDERED script and run with `k3s` stubbed as a shell
function that logs each call and answers from canned output.

  * `write_mirror_keep_set()` fails closed. The offline prune keeps only the N newest revisions
    plus this set, so a set written from a failed read would let rank alone delete images live
    pods run.
  * `restore_mirror_store()` journals the prune's lines before anything else, and deletes the
    mirror Job only when it has not finished, so a failure keeps the log its message names.
  * the mirror's phase scales only its own Deployment, never the push store (the operator's
    ruling on #5680), whose own GC is retired.
  * the stamp `ghcr-mirror-down` exists from before the scale-down until the mirror serves
    again, and a run that finds it restores the mirror first.
  * the deploy's restore takes the lock the script holds, and backs off while it is held
    (#5736).
"""

import os
import shlex
from pathlib import Path

from _helpers import load_tasks
from _shell_render import rendered_shell_function, rendered_shell_text
from lib import yaml_fast
from lib.proc_testing import fake_bin, path_with, run
from lib.repo_paths import REPO

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


def test_tag_only_ghcr_refs_are_written_beside_the_digests(tmp_path):
    """A flux-owned image runs by tag alone, so a scaled-to-zero one leaves only its tag."""
    body = f'{{"items": [{{"imageID": "ghcr.io/x@{LIVE}"}}, {{"image": "ghcr.io/danielh2018/nut:abc123"}}]}}'
    calls, note = _write(tmp_path, 0, body)

    assert note == ""
    written = "\n".join(calls)
    assert f"--from-literal=digests={LIVE}\ndanielh2018/nut:abc123" in written, calls


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


RESTORE = rendered_shell_function(
    "k8s", "registry", "registry-gc.sh.j2", "restore_mirror_store"
)
START = rendered_shell_function("k8s", "registry", "registry-gc.sh.j2", "start_deploy")
STOP = rendered_shell_function("k8s", "registry", "registry-gc.sh.j2", "stop_deploy")
JOURNAL = rendered_shell_function(
    "k8s", "registry", "registry-gc.sh.j2", "journal_mirror_prune"
)
REPORT = rendered_shell_function(
    "k8s", "registry", "registry-gc.sh.j2", "report_mirror_job"
)
COLLECT_MIRROR = rendered_shell_function(
    "k8s", "registry", "registry-gc.sh.j2", "collect_mirror_store"
)
STRANDED = rendered_shell_function(
    "k8s", "registry", "registry-gc.sh.j2", "restore_stranded_mirror"
)
PRUNED_LINE = f"pruned danielh2018/nut@{LIVE} tags=none"

# Every phase test runs with these, the values the rendered script assigns.
_VARS = """
KUBECTL="k3s kubectl -n homelab"
NS=homelab
MIRROR_DEPLOY=ghcr-mirror
MIRROR_JOB=registry-gc-ghcr-mirror
MIRROR_KEEP_CM=registry-gc-ghcr-mirror-keep
MIRROR_JOB_MANIFEST=/m/ghcr-mirror-gc-job.yaml
MIRROR_STARTED=0
MIRROR_DONE=0
MIRROR_DOWN=0
MIRROR_NOTE=""
MIRROR_MSG=""
"""


def _state(tmp_path: Path) -> str:
    """The stamp's variables, pointed into `tmp_path`."""
    return f"STATE_DIR={shlex.quote(str(tmp_path))}\nMIRROR_STAMP=$STATE_DIR/ghcr-mirror-down\n"


def _restore(tmp_path: Path, finished: str) -> list[str]:
    """Run restore_mirror_store after a mirror phase that stopped early; return every call."""
    calls = tmp_path / "calls"
    script = f"""
{_VARS}
{_state(tmp_path)}
MIRROR_STARTED=1
MIRROR_DOWN=1
k3s() {{
  printf '%s\\n' "$*" >> {shlex.quote(str(calls))}
  case "$*" in
    *" logs "*) printf 'ghcr-mirror prune: 1 revision(s)\\n%s\\n' "$PRUNED_LINE" ;;
    *" get job "*) printf '%s' "$FINISHED" ;;
  esac
}}
logger() {{ printf 'logger %s\\n' "$(cat)" >> {shlex.quote(str(calls))}; }}
{JOURNAL}
{START}
{RESTORE}
restore_mirror_store
"""
    env = {**os.environ, "FINISHED": finished, "PRUNED_LINE": PRUNED_LINE}
    run(["bash", "-c", script], env=env, check=True)
    return calls.read_text().splitlines()


def _index(calls: list[str], needle: str) -> int:
    (i,) = [n for n, c in enumerate(calls) if needle in c]
    return i


def test_an_unfinished_mirror_job_is_journaled_then_deleted_before_scale_up(tmp_path):
    calls = _restore(tmp_path, finished="")

    assert f"logger {PRUNED_LINE}" in calls, calls
    journal = _index(calls, f"logger {PRUNED_LINE}")
    delete = _index(calls, "delete job registry-gc-ghcr-mirror")
    scale = _index(calls, "scale deploy/ghcr-mirror --replicas=1")
    assert journal < delete < scale, calls
    assert not any("deploy/registry" in c for c in calls), calls


def test_a_finished_mirror_job_is_journaled_and_kept_for_its_log(tmp_path):
    calls = _restore(tmp_path, finished="1")

    assert f"logger {PRUNED_LINE}" in calls, calls
    assert not any("delete job" in c for c in calls), calls


def _phase(tmp_path: Path, function: str) -> list[str]:
    """Run the mirror's whole GC phase against a cluster where every call succeeds.

    Each call is logged with whether the stamp existed when it was made.
    """
    calls = tmp_path / "calls"
    body = f'{{"items": [{{"imageID": "ghcr.io/x@{LIVE}"}}]}}'
    script = f"""
{_VARS}
{_state(tmp_path)}
k3s() {{
  printf '%s stamp=%s\\n' "$*" "$([[ -e $MIRROR_STAMP ]] && echo 1 || echo 0)" \\
    >> {shlex.quote(str(calls))}
  case "$*" in
    *" get job "*) ;;
    *" get "*) printf '%s' "$GET_BODY" ;;
    *" logs "*" prune"*) printf '%s\\n' "$PRUNED_LINE" ;;
  esac
}}
logger() {{ cat > /dev/null; }}
{KEEP_SET}
{STOP}
{START}
{JOURNAL}
{REPORT}
{RESTORE}
{COLLECT_MIRROR}
{STRANDED}
{function}
"""
    env = {**os.environ, "GET_BODY": body, "PRUNED_LINE": PRUNED_LINE}
    run(["bash", "-c", script], env=env, check=True)
    return calls.read_text().splitlines() if calls.exists() else []


def test_the_mirror_phase_scales_only_the_mirror_deployment(tmp_path):
    """The operator's ruling on #5680: the mirror's GC never scales the push store."""
    calls = _phase(tmp_path, "collect_mirror_store")

    down = _index(calls, "scale deploy/ghcr-mirror --replicas=0")
    applied = _index(calls, "apply -f /m/ghcr-mirror-gc-job.yaml")
    assert down < applied, calls
    assert any("wait --for=delete pod -l app=ghcr-mirror" in c for c in calls), calls
    assert not any("deploy/registry" in c or "app=registry " in c for c in calls), calls


def test_the_stamp_spans_the_scale_down_to_the_restore(tmp_path):
    """A run killed anywhere in the window leaves the stamp the next run repairs from."""
    calls = _phase(tmp_path, "collect_mirror_store && restore_mirror_store")

    assert calls[_index(calls, "--replicas=0")].endswith("stamp=1"), calls
    assert calls[_index(calls, "scale deploy/ghcr-mirror --replicas=1")].endswith(
        "stamp=1"
    ), calls
    assert not (tmp_path / "ghcr-mirror-down").exists()


def test_a_run_that_finds_the_stamp_restores_the_mirror_first(tmp_path):
    (tmp_path / "ghcr-mirror-down").touch()
    calls = _phase(tmp_path, "restore_stranded_mirror")

    delete = _index(calls, "delete job registry-gc-ghcr-mirror")
    scale = _index(calls, "scale deploy/ghcr-mirror --replicas=1")
    assert delete < scale, calls
    assert not (tmp_path / "ghcr-mirror-down").exists()


def test_a_run_with_no_stamp_restores_nothing(tmp_path):
    assert _phase(tmp_path, "restore_stranded_mirror") == []


TASKS = REPO / "ansible/roles/k8s/registry/tasks/main.yml"
RESTORE_TASK = "Restore a ghcr-mirror replica a stopped GC left at zero"


def _deploy_restore(tmp_path: Path, lock: Path, held: bool) -> tuple[int, list[str]]:
    """Run the deploy's restore command as the task renders it; return its rc and calls."""
    (task,) = [t for t in load_tasks(TASKS) if t.get("name") == RESTORE_TASK]
    cmd = task["ansible.builtin.command"]["cmd"]
    cmd = cmd.replace("{{ registry_k8s_gc_lock_file }}", str(lock))
    cmd = cmd.replace("{{ k8s_namespace }}", "homelab")
    assert "{{" not in cmd, cmd
    calls = tmp_path / "calls"
    bin_dir = fake_bin(
        tmp_path / "bin", k3s=f'echo "$*" >> {shlex.quote(str(calls))}\n'
    )
    hold = f"exec 9>{shlex.quote(str(lock))}; flock 9; " if held else ""
    env = {**os.environ, "PATH": path_with(bin_dir)}
    done = run(["bash", "-c", hold + cmd], env=env, check=False)
    return done.returncode, calls.read_text().splitlines() if calls.exists() else []


def test_the_deploy_restore_backs_off_while_a_gc_run_holds_the_lock(tmp_path):
    """#5736: the GC holds the lock before its scale-down, so the deploy cannot undo it."""
    rc, calls = _deploy_restore(tmp_path, tmp_path / "registry-gc.lock", held=True)

    assert rc == 75
    assert calls == []


def test_the_deploy_restore_scales_the_mirror_when_no_gc_runs(tmp_path):
    rc, calls = _deploy_restore(tmp_path, tmp_path / "registry-gc.lock", held=False)

    assert rc == 0
    assert calls == [
        "kubectl -n homelab scale deploy/ghcr-mirror --current-replicas=0 --replicas=1"
    ]


def test_the_script_and_the_deploy_take_the_same_lock():
    defaults = yaml_fast.safe_load(
        (REPO / "ansible/roles/k8s/registry/defaults/main.yml").read_text()
    )
    script = rendered_shell_text("k8s", "registry", "registry-gc.sh.j2")
    (task,) = [t for t in load_tasks(TASKS) if t.get("name") == RESTORE_TASK]

    assert f"LOCK_FILE={defaults['registry_k8s_gc_lock_file']}\n" in script
    assert (
        "flock --nonblock --conflict-exit-code 75 {{ registry_k8s_gc_lock_file }}"
        in (task["ansible.builtin.command"]["cmd"])
    )


def _report(tmp_path: Path, note: str) -> tuple[int, str]:
    """Run report_mirror_job after a completed mirror Job; return its rc and MIRROR_MSG."""
    script = f"""
{_VARS}
MIRROR_NOTE="$NOTE"
k3s() {{ case "$*" in *" logs "*" prune"*) printf '%s\\n' "$PRUNED_LINE" ;; esac; }}
logger() {{ cat > /dev/null; }}
{JOURNAL}
{REPORT}
report_mirror_job
rc=$?
printf '%s\\n%s' "$rc" "$MIRROR_MSG"
"""
    env = {**os.environ, "NOTE": note, "PRUNED_LINE": PRUNED_LINE}
    rc, msg = run(["bash", "-c", script], env=env, check=True).stdout.split("\n", 1)
    return int(rc), msg


def test_a_completed_prune_keeps_the_run_up(tmp_path):
    rc, msg = _report(tmp_path, "")

    assert rc == 0, msg
    assert "ghcr-mirror: 1 revision(s) pruned" in msg, msg


def test_a_skipped_prune_reports_the_run_down(tmp_path):
    """A persistent keep-set fault would otherwise skip every week behind a green tile."""
    note = "ghcr-mirror prune skipped, workloads unreadable"
    rc, msg = _report(tmp_path, note)

    assert rc == 1, msg
    assert note in msg, msg
