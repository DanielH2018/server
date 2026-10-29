"""registry-gc's second phase, which prunes and warms the dockerhub-mirror store (#6536).

The functions are sliced out of the RENDERED script and run with `k3s`, `curl` and `logger`
stubbed as shell functions that log each call, as test_registry_gc_ghcr_mirror.py does for the
ghcr-mirror's phase.

  * `dockerhub_refs()` reads docker.io refs, which carry no host to grep for, and names each
    by the path the mirror caches it under.
  * the run collects the dockerhub-mirror only after the ghcr-mirror serves again, so it takes
    down one mirror at a time, and its message reports both phases.
  * a stamp a killed run left for the dockerhub-mirror is repaired in that mirror's phase.
"""

import json
import os
import shlex
from pathlib import Path

from _shell_render import rendered_shell_function, rendered_shell_text
from lib.proc_testing import run

_SCRIPT = rendered_shell_text("setup", "k8s_host_files", "registry-gc.sh.j2")
FUNCTIONS = "\n".join(
    rendered_shell_function("setup", "k8s_host_files", "registry-gc.sh.j2", name)
    for name in (
        "use_dockerhub_store",
        "ghcr_refs",
        "dockerhub_refs",
        "write_mirror_keep_set",
        "stop_deploy",
        "start_deploy",
        "journal_mirror_prune",
        "report_mirror_job",
        "restore_mirror_store",
        "scale_up_and_report",
        "collect_mirror_store",
        "mirror_manifest",
        "mirror_blob_digests",
        "warm_mirror",
        "restore_stranded_mirror",
        "run_mirror_phase",
        "take_lock",
    )
)
# The script's top-level sequence, everything after take_lock's definition, as cron runs it.
MAIN = _SCRIPT[_SCRIPT.index("\n}\n", _SCRIPT.index("take_lock() {")) + 3 :]
DIGEST = "sha256:" + "a" * 64
LAYER = "sha256:" + "b" * 64

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
MIRROR_STARTED=0
MIRROR_DONE=0
MIRROR_DOWN=0
MIRROR_NOTE=""
MIRROR_MSG=""
DONE_MSG=""
FAILED=0
COMPLETED=0
MIRROR_URL=http://127.0.0.1:5002
ACCEPT=x
WARM_BUDGET_S=60
WARM_REFS=""
"""


def _refs(body: dict) -> list[str]:
    script = f'{FUNCTIONS}\ndockerhub_refs <<<"$BODY"\n'
    env = {**os.environ, "BODY": json.dumps(body)}
    return run(["bash", "-c", script], env=env, check=True).stdout.splitlines()


def test_dockerhub_refs_name_each_docker_io_image_by_its_cached_path():
    body = {
        "items": [
            {"image": "alpine:3.24"},
            {"image": f"prom/node-exporter:v1.12.1@{DIGEST}"},
            {"imageID": f"docker.io/library/registry@{DIGEST}"},
            {"image": "index.docker.io/grafana/grafana:12.0"},
            {"image": "ghcr.io/danielh2018/nut:abc123"},
            {"image": "registry.k8s.io/pause:3.10"},
            {"image": "localhost/built:1"},
            {"image": "10.43.0.50:5000/seed:1"},
            {"imageID": DIGEST},
            {"name": "alpine:3.24-not-an-image-field"},
        ]
    }

    assert _refs(body) == [
        "grafana/grafana:12.0",
        "library/alpine:3.24",
        f"library/registry@{DIGEST}",
        f"prom/node-exporter:v1.12.1@{DIGEST}",
    ]


def _run(tmp_path: Path) -> tuple[list[str], str]:
    """Run the whole top-level sequence; return every stubbed call and the reported result."""
    calls = tmp_path / "calls"
    body = {
        "items": [
            {"imageID": f"ghcr.io/x@{DIGEST}"},
            {"image": "alpine:3.24"},
        ]
    }
    manifest = json.dumps({"config": {"digest": LAYER}, "layers": []})
    script = f"""
{_VARS}
STATE_DIR={shlex.quote(str(tmp_path))}
MIRROR_STAMP=$STATE_DIR/ghcr-mirror-down
LOCK_FILE={shlex.quote(str(tmp_path / "registry-gc.lock"))}
k3s() {{
  printf '%s stamps=%s%s\\n' "$*" \\
    "$([[ -e $STATE_DIR/ghcr-mirror-down ]] && echo g)" \\
    "$([[ -e $STATE_DIR/dockerhub-mirror-down ]] && echo d)" >> {shlex.quote(str(calls))}
  case "$*" in
    *" get job "*) ;;
    *" get "*) printf '%s' "$GET_BODY" ;;
    *" logs "*" prune"*) printf 'pruned library/old@%s tags=none\\n' "$DIGEST" ;;
  esac
}}
curl() {{
  printf 'curl %s\\n' "${{@: -1}}" >> {shlex.quote(str(calls))}
  case "${{@: -1}}" in
    */manifests/*) printf '%s' "$MANIFEST" ;;
    */blobs/*) printf 1000000 ;;
  esac
}}
logger() {{ cat > /dev/null; }}
finish() {{ printf '%s|%s' "$STATUS" "$MSG" > {shlex.quote(str(tmp_path / "result"))}; }}
{FUNCTIONS}
trap scale_up_and_report EXIT
{MAIN}
"""
    env = {
        **os.environ,
        "GET_BODY": json.dumps(body),
        "MANIFEST": manifest,
        "DIGEST": DIGEST,
    }
    run(["bash", "-c", script], env=env, check=False)
    return calls.read_text().splitlines(), (tmp_path / "result").read_text()


def _index(calls: list[str], needle: str) -> int:
    (i,) = [n for n, c in enumerate(calls) if needle in c]
    return i


def test_the_dockerhub_mirror_is_collected_only_once_the_ghcr_mirror_serves(tmp_path):
    calls, result = _run(tmp_path)

    ghcr_up = _index(calls, "scale deploy/ghcr-mirror --replicas=1")
    down = _index(calls, "scale deploy/dockerhub-mirror --replicas=0")
    applied = _index(calls, "apply -f /m/dockerhub-mirror-gc-job.yaml")
    up = _index(calls, "scale deploy/dockerhub-mirror --replicas=1")
    warm = _index(calls, "curl http://127.0.0.1:5005/v2/library/alpine/manifests/3.24")
    assert ghcr_up < down < applied < up < warm, calls
    # Only the dockerhub-mirror's stamp stands while it is down.
    assert calls[down].endswith(" stamps=d"), calls
    keep = [
        c for c in calls if "create configmap registry-gc-dockerhub-mirror-keep" in c
    ]
    assert len(keep) == 1 and "library/alpine:3.24" in "\n".join(calls), calls
    assert not (tmp_path / "dockerhub-mirror-down").exists()

    status, msg = result.split("|", 1)
    assert status == "up", msg
    assert "ghcr-mirror: 1 revision(s) pruned" in msg, msg
    assert "; dockerhub-mirror: 1 revision(s) pruned" in msg, msg


def test_a_stranded_dockerhub_mirror_is_restored_before_its_phase(tmp_path):
    (tmp_path / "dockerhub-mirror-down").touch()
    calls, _ = _run(tmp_path)

    first = [c for c in calls if " scale deploy/dockerhub-mirror " in c]
    assert "--replicas=1" in first[0], calls
    delete = _index(
        calls, "delete job registry-gc-dockerhub-mirror --ignore-not-found --cascade"
    )
    assert delete < calls.index(first[0]), calls
