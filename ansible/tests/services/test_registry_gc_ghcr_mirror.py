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
  * the run holds the lock from before its scale-down until after its restore (#5736). The
    deploy-time restore that took the same lock was deleted with registry's Ansible tasks.
  * lscr.io refs join the keep set and the warm list under their ghcr.io repository path,
    because registries.yaml points lscr.io at the same mirror (#6257).
  * `warm_mirror()` GETs each distinct amd64 blob of every ghcr.io ref through the mirror,
    only after the restore succeeded, and a blob that fails to warm leaves the run up (#5654).
"""

import json
import os
import re
import shlex
from pathlib import Path

from _shell_render import rendered_shell_function, rendered_shell_text
from lib.proc_testing import run

KEEP_SET = "\n".join(
    rendered_shell_function("setup", "k8s_host_files", "registry-gc.sh.j2", name)
    for name in ("ghcr_refs", "write_mirror_keep_set")
)
LIVE = "sha256:" + "a" * 64
PINNED = "sha256:" + "b" * 64


def _write(tmp_path: Path, get_rc: int, get_body: str) -> tuple[list[str], str]:
    """Run write_mirror_keep_set once; return the kubectl calls it made and MIRROR_NOTE."""
    calls = tmp_path / "calls"
    script = f"""
KUBECTL="k3s kubectl -n homelab"
MIRROR_KEEP_CM=registry-gc-ghcr-mirror-keep
MIRROR_REFS=ghcr_refs
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


def test_lscr_refs_are_kept_and_warmed_under_their_ghcr_path(tmp_path):
    """registries.yaml sends lscr.io pulls to the ghcr-mirror, which caches them as ghcr.io paths.

    A keep set without them would let the prune delete a digest-pinned lscr.io image (#6257).
    """
    script = f"""
KUBECTL="k3s kubectl -n homelab"
MIRROR_KEEP_CM=registry-gc-ghcr-mirror-keep
MIRROR_REFS=ghcr_refs
MIRROR_NOTE=""
WARM_REFS=""
k3s() {{ printf '%s\\n' "$*" >> {shlex.quote(str(tmp_path / "calls"))}
  [[ "$*" == *" get "* ]] && printf '%s' "$GET_BODY"; return 0; }}
{KEEP_SET}
write_mirror_keep_set
printf '%s' "$WARM_REFS"
"""
    body = (
        f'{{"items": [{{"image": "lscr.io/linuxserver/sonarr:4.0@{PINNED}"}},'
        f' {{"image": "lscr.io/linuxserver/radarr:5.1"}}]}}'
    )
    env = {**os.environ, "GET_BODY": body}
    refs = run(["bash", "-c", script], env=env, check=True).stdout.splitlines()

    assert refs == ["linuxserver/radarr:5.1", f"linuxserver/sonarr@{PINNED}"], refs
    kept = (tmp_path / "calls").read_text().splitlines()
    assert {"linuxserver/radarr:5.1", "linuxserver/sonarr:4.0"} <= set(kept), kept


def test_warm_refs_keep_the_digest_and_drop_a_bare_repository(tmp_path):
    script = f"""
KUBECTL="k3s kubectl -n homelab"
MIRROR_KEEP_CM=registry-gc-ghcr-mirror-keep
MIRROR_REFS=ghcr_refs
MIRROR_NOTE=""
WARM_REFS=""
k3s() {{ [[ "$*" == *" get "* ]] && printf '%s' "$GET_BODY"; return 0; }}
{KEEP_SET}
write_mirror_keep_set
printf '%s' "$WARM_REFS"
"""
    body = (
        f'{{"items": [{{"image": "ghcr.io/y:latest@{PINNED}"}},'
        f' {{"image": "ghcr.io/danielh2018/nut:abc123"}},'
        f' {{"imageID": "ghcr.io/x@{LIVE}"}}, {{"note": "see ghcr.io/bare for docs"}}]}}'
    )
    env = {**os.environ, "GET_BODY": body}
    refs = run(["bash", "-c", script], env=env, check=True).stdout.splitlines()

    assert refs == ["danielh2018/nut:abc123", f"x@{LIVE}", f"y@{PINNED}"], refs


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
    "setup", "k8s_host_files", "registry-gc.sh.j2", "restore_mirror_store"
)
START = rendered_shell_function(
    "setup", "k8s_host_files", "registry-gc.sh.j2", "start_deploy"
)
STOP = rendered_shell_function(
    "setup", "k8s_host_files", "registry-gc.sh.j2", "stop_deploy"
)
JOURNAL = rendered_shell_function(
    "setup", "k8s_host_files", "registry-gc.sh.j2", "journal_mirror_prune"
)
REPORT = rendered_shell_function(
    "setup", "k8s_host_files", "registry-gc.sh.j2", "report_mirror_job"
)
COLLECT_MIRROR = rendered_shell_function(
    "setup", "k8s_host_files", "registry-gc.sh.j2", "collect_mirror_store"
)
TAKE_LOCK = rendered_shell_function(
    "setup", "k8s_host_files", "registry-gc.sh.j2", "take_lock"
)
# The script's top-level sequence, everything after take_lock's definition, as cron runs it.
_SCRIPT = rendered_shell_text("setup", "k8s_host_files", "registry-gc.sh.j2")
MAIN = _SCRIPT[_SCRIPT.index("\n}\n", _SCRIPT.index("take_lock() {")) + 3 :]
STRANDED = "\n".join(
    rendered_shell_function("setup", "k8s_host_files", "registry-gc.sh.j2", name)
    for name in (
        "restore_stranded_mirror",
        "run_mirror_phase",
        "use_dockerhub_store",
        "dockerhub_refs",
    )
)
PRUNED_LINE = f"pruned danielh2018/nut@{LIVE} tags=none"

# Every phase test runs with these, the values the rendered script assigns.
_VARS = """
KUBECTL="k3s kubectl -n homelab"
NS=homelab
MIRROR_DEPLOY=ghcr-mirror
MIRROR_UPSTREAM=ghcr.io
MIRROR_REFS=ghcr_refs
MIRROR_JOB=registry-gc-ghcr-mirror
MIRROR_KEEP_CM=registry-gc-ghcr-mirror-keep
MIRROR_JOB_MANIFEST=/m/ghcr-mirror-gc-job.yaml
DOCKERHUB_JOB_MANIFEST=/m/dockerhub-mirror-gc-job.yaml
DONE_MSG=""
MIRROR_STARTED=0
MIRROR_DONE=0
MIRROR_DOWN=0
MIRROR_NOTE=""
MIRROR_MSG=""
MIRROR_URL=http://127.0.0.1:5002
ACCEPT=x
WARM_BUDGET_S=60
WARM_REFS=""
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


def _phase(
    tmp_path: Path, function: str, rollout_rc: int = 0, rc: int = 0
) -> list[str]:
    """Run the mirror's whole GC phase against a cluster where every call succeeds.

    Each call is logged with whether the GC lock was held and whether the stamp existed
    when it was made.
    """
    calls = tmp_path / "calls"
    body = f'{{"items": [{{"imageID": "ghcr.io/x@{LIVE}"}}]}}'
    script = f"""
{_VARS}
{_state(tmp_path)}
LOCK_FILE={shlex.quote(str(tmp_path / "registry-gc.lock"))}
k3s() {{
  printf '%s lock=%s stamp=%s\\n' "$*" \\
    "$(flock -n "$LOCK_FILE" true 2>/dev/null && echo 0 || echo 1)" \\
    "$([[ -e $MIRROR_STAMP ]] && echo 1 || echo 0)" >> {shlex.quote(str(calls))}
  case "$*" in
    *" get job "*) ;;
    *" get "*) printf '%s' "$GET_BODY" ;;
    *" logs "*" prune"*) printf '%s\\n' "$PRUNED_LINE" ;;
    *" rollout status "*) return "${{ROLLOUT_RC:-0}}" ;;
  esac
}}
logger() {{ cat > /dev/null; }}
{_curl_stub(calls)}
{WARM}
{KEEP_SET}
{STOP}
{START}
{JOURNAL}
{REPORT}
{RESTORE}
{COLLECT_MIRROR}
{STRANDED}
{TAKE_LOCK}
{function}
"""
    env = {
        **os.environ,
        "GET_BODY": body,
        "PRUNED_LINE": PRUNED_LINE,
        "ROLLOUT_RC": str(rollout_rc),
    }
    assert run(["bash", "-c", script], env=env, check=False).returncode == rc
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


def test_the_run_holds_the_lock_from_the_scale_down_to_the_restore(tmp_path):
    """#5736: the deploy's restore backs off only while the lock is held, so it must span both."""
    calls = _phase(tmp_path, MAIN)

    down = calls[_index(calls, "scale deploy/ghcr-mirror --replicas=0")]
    up = calls[_index(calls, "scale deploy/ghcr-mirror --replicas=1")]
    assert " lock=1 " in down, calls
    assert " lock=1 " in up, calls


def test_the_run_warms_the_mirror_only_after_it_serves_again(tmp_path):
    calls = _phase(tmp_path, MAIN)

    up = _index(calls, "scale deploy/ghcr-mirror --replicas=1")
    warm = _index(calls, f"curl http://127.0.0.1:5002/v2/x/manifests/{LIVE}")
    assert up < warm, calls


def test_a_mirror_that_did_not_come_back_is_not_warmed(tmp_path):
    calls = _phase(tmp_path, MAIN, rollout_rc=1, rc=1)

    assert any("--replicas=1" in c for c in calls), calls
    assert not any(c.startswith("curl ") for c in calls), calls
    # Its stamp stays, so the dockerhub-mirror is not taken down beside it (#6536).
    assert not any("dockerhub-mirror" in c for c in calls), calls


def test_a_run_that_finds_the_stamp_restores_the_mirror_first(tmp_path):
    (tmp_path / "ghcr-mirror-down").touch()
    calls = _phase(tmp_path, "restore_stranded_mirror")

    delete = _index(calls, "delete job registry-gc-ghcr-mirror")
    scale = _index(calls, "scale deploy/ghcr-mirror --replicas=1")
    assert delete < scale, calls
    assert not (tmp_path / "ghcr-mirror-down").exists()


def test_a_run_with_no_stamp_restores_nothing(tmp_path):
    assert _phase(tmp_path, "restore_stranded_mirror") == []


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


WARM = "\n".join(
    rendered_shell_function("setup", "k8s_host_files", "registry-gc.sh.j2", name)
    for name in ("mirror_manifest", "mirror_blob_digests", "warm_mirror")
)
CHILD, ARM, CONFIG, SHARED, OWN = ("sha256:" + c * 64 for c in "cdefg")
# The script's own Accept assignment, so the stub sees the header the script really sends.
(_ACCEPT,) = [line for line in _SCRIPT.splitlines() if line.startswith("ACCEPT=")]
_MANIFESTS = {
    "idx/manifests/1": {
        "manifests": [
            {"digest": ARM, "platform": {"os": "linux", "architecture": "arm64"}},
            {"digest": CHILD, "platform": {"os": "linux", "architecture": "amd64"}},
        ]
    },
    f"idx/manifests/{CHILD}": {
        "config": {"digest": CONFIG},
        "layers": [{"digest": SHARED}],
    },
    "plain/manifests/2": {
        "config": {"digest": CONFIG},
        "layers": [{"digest": SHARED}, {"digest": OWN}],
    },
}


def _curl_stub(calls: Path) -> str:
    """A `curl` that logs each URL, answers _MANIFESTS, and fails a blob named in FAIL_BLOB.

    A manifest is served only to a request that accepts an OCI index, as a registry serves an
    index only to a client that names its type, so a dropped Accept header fails the test.
    """
    arms = "\n".join(
        f"    */v2/{path}) printf '%s' {shlex.quote(json.dumps(body))} ;;"
        for path, body in _MANIFESTS.items()
    )
    return f"""
curl() {{
  local url=${{@: -1}} accept="" arg
  for arg; do [[ "$arg" == Accept:* ]] && accept=$arg; done
  printf 'curl %s\\n' "$url" >> {shlex.quote(str(calls))}
  [[ "$url" != */manifests/* || "$accept" == *image.index.v1+json* ]] || return 22
  case "$url" in
{arms}
    */manifests/*) return 22 ;;
    *"${{FAIL_BLOB:-none}}") return 22 ;;
    */blobs/*) printf 1000000 ;;
  esac
}}
"""


def _warm(
    tmp_path: Path, refs: str, fail_blob: str = "", budget: int = 60
) -> tuple[list[str], str]:
    """Run warm_mirror over `refs`; return the URLs curl was asked for and MIRROR_MSG."""
    calls = tmp_path / "calls"
    script = f"""
MIRROR_URL=http://127.0.0.1:5002
MIRROR_DEPLOY=ghcr-mirror
MIRROR_UPSTREAM=ghcr.io
{_ACCEPT}
WARM_BUDGET_S={budget}
WARM_REFS="$REFS"
MIRROR_MSG="ghcr-mirror: 0 revision(s) pruned"
logger() {{ printf 'logger %s\\n' "$*" >> {shlex.quote(str(calls))}; }}
{_curl_stub(calls)}
{WARM}
warm_mirror
printf '%s' "$MIRROR_MSG"
"""
    env = {**os.environ, "REFS": refs, "FAIL_BLOB": fail_blob}
    msg = run(["bash", "-c", script], env=env, check=True).stdout
    return calls.read_text().splitlines() if calls.exists() else [], msg


def test_warm_gets_each_distinct_amd64_blob_once_through_the_mirror(tmp_path):
    calls, msg = _warm(tmp_path, "idx:1\nplain@2")

    blobs = [c.removeprefix("curl ") for c in calls if "/blobs/" in c]
    assert blobs == [
        f"http://127.0.0.1:5002/v2/idx/blobs/{CONFIG}",
        f"http://127.0.0.1:5002/v2/idx/blobs/{SHARED}",
        f"http://127.0.0.1:5002/v2/plain/blobs/{OWN}",
    ], calls
    assert not any(ARM in c for c in calls), calls
    assert re.fullmatch(
        r"ghcr-mirror: 0 revision\(s\) pruned; warmed 3 blob\(s\), 3 MB in \d+s", msg
    ), msg


def test_a_blob_that_fails_to_warm_is_named_and_the_rest_still_warm(tmp_path):
    calls, msg = _warm(tmp_path, "idx:1\nplain@2\nmissing:9", fail_blob=OWN)

    journal = [c for c in calls if c.startswith("logger ")]
    assert any(f"plain@{OWN}" in c for c in journal), journal
    assert any("missing 9" in c for c in journal), journal
    assert "warmed 2 blob(s)" in msg, msg
    assert ", 2 failed (journalctl -t registry-gc)" in msg, msg


def test_warm_starts_no_blob_once_the_budget_is_spent(tmp_path):
    calls, msg = _warm(tmp_path, "plain@2", budget=0)

    assert not any("/blobs/" in c for c in calls), calls
    assert "warmed 0 blob(s)" in msg, msg
    assert "3 skipped past the 0s budget" in msg, msg


def test_no_ref_warms_nothing_and_says_so(tmp_path):
    calls, msg = _warm(tmp_path, "")

    assert calls == [], calls
    assert msg.endswith("; mirror not warmed, no ghcr.io ref read"), msg
