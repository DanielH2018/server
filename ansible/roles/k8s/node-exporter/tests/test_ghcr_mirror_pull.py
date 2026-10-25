"""ghcr-mirror-pull.sh reports whether a node's ghcr.io mirror serves nut's manifest (#5542).

The script runs as the node-exporter DaemonSet's `ghcr-mirror-pull` sidecar and writes a
textfile check file the exporter reads from a third `--collector.textfile.directory`. These
tests run it once under `sh` against a local HTTP server standing in for the mirror, with the
host's wget, which takes the flags busybox's does. They read the rendered DaemonSet and nut's
ConfigMap for the wiring.

Run: uv run pytest ansible/roles/k8s/node-exporter/tests/test_ghcr_mirror_pull.py
"""

import re
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from _helpers import K8S_ROLES
from _k8s_render import rendered_k8s_text
from lib import yaml_fast
from lib.proc_testing import run

SCRIPT = K8S_ROLES / "node-exporter" / "files" / "ghcr-mirror-pull.sh"
RULES = K8S_ROLES / "observability" / "files" / "rules" / "registry.yaml"
SERVED = "/v2/danielh2018/nut/manifests/sha-0123456789ab"
# A port nothing listens on: the endpoint of the side of the node split a test must not use.
DEAD = "http://127.0.0.1:9"


class _Mirror(BaseHTTPRequestHandler):
    """Answers 200 for `SERVED` when the request accepts a Docker manifest, 404 otherwise."""

    def do_GET(self) -> None:
        accept = self.headers.get("Accept", "")
        ok = self.path == SERVED and (
            "application/vnd.docker.distribution.manifest.v2+json" in accept
        )
        self.send_response(200 if ok else 404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture(scope="module")
def mirror() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Mirror)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture()
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    target, out = tmp_path / "target", tmp_path / "out"
    target.mkdir()
    out.mkdir()
    (target / "repository").write_text("danielh2018/nut")
    (target / "tag").write_text("sha-0123456789ab")
    return target, out


def _write_once(
    target: Path, out: Path, node: str, local: str, remote: str
) -> dict[str, str]:
    """Run the script once on `node`, with daniel-box as the registry node."""
    env = {
        "NODE_NAME": node,
        "REGISTRY_NODE": "daniel-box",
        "LOCAL_ENDPOINT": local,
        "REMOTE_ENDPOINT": remote,
        "TARGET_DIR": str(target),
        "TEXTFILE_DIR": str(out),
        "TIMEOUT_S": "5",
        "ONCE": "1",
        "PATH": "/usr/bin:/bin",
    }
    result = run(["sh", str(SCRIPT)], env=env, check=False)
    assert result.returncode == 0, result.stderr
    assert [p.name for p in out.iterdir()] == ["ghcr-mirror-pull.prom"]
    assert (out / "ghcr-mirror-pull.prom").stat().st_mode & 0o777 == 0o644
    text = (out / "ghcr-mirror-pull.prom").read_text()
    return dict(
        line.rsplit(" ", 1) for line in text.splitlines() if not line.startswith("#")
    )


SUCCESS = 'homelab_check_success{check="ghcr-mirror-pull"}'


def test_a_served_manifest_keeps_the_check_green_on_each_side_of_the_node_split(
    dirs, mirror
):
    target, out = dirs
    assert _write_once(target, out, "daniel-box", mirror, DEAD)[SUCCESS] == "1"
    assert _write_once(target, out, "daniel-server", DEAD, mirror)[SUCCESS] == "1"


def test_a_refused_endpoint_turns_the_check_red(dirs, mirror):
    target, out = dirs
    assert _write_once(target, out, "daniel-server", mirror, DEAD)[SUCCESS] == "0"


def test_a_tag_the_mirror_does_not_serve_turns_the_check_red(dirs, mirror):
    target, out = dirs
    (target / "tag").write_text("sha-ffffffffffff")
    assert _write_once(target, out, "daniel-server", DEAD, mirror)[SUCCESS] == "0"


def test_a_missing_tag_file_writes_a_red_verdict_rather_than_none(dirs, mirror):
    target, out = dirs
    (target / "tag").unlink()
    assert _write_once(target, out, "daniel-server", DEAD, mirror)[SUCCESS] == "0"


def test_both_not_reporting_rules_carry_the_scripts_deadline(dirs, mirror):
    target, out = dirs
    deadline = _write_once(target, out, "daniel-server", DEAD, mirror)[
        'homelab_check_deadline_seconds{check="ghcr-mirror-pull"}'
    ]
    [group] = yaml_fast.safe_load(RULES.read_text())["groups"]
    exprs = [
        r["expr"]
        for r in group["rules"]
        if r.get("alert") == "GhcrMirrorPullNotReporting"
    ]
    assert len(exprs) == 2
    for expr in exprs:
        assert re.findall(r"> (\d+)", expr) == [deadline]
        assert re.findall(r"\[(\d+)s\]", expr) == [deadline]


def test_the_daemonset_reads_the_tag_nut_renders_and_the_exporter_reads_its_file():
    [ds] = [
        d
        for d in yaml_fast.safe_load_all(
            rendered_k8s_text("node-exporter", "daemonset.yaml.j2")
        )
        if d
    ]
    pod = ds["spec"]["template"]["spec"]
    containers = {c["name"]: c for c in pod["containers"]}
    sidecar, exporter = containers["ghcr-mirror-pull"], containers["node-exporter"]
    assert sidecar["command"][-1].strip() == SCRIPT.read_text().strip()
    volumes = {v["name"]: v for v in pod["volumes"]}
    mounts = {m["mountPath"]: m["name"] for m in sidecar["volumeMounts"]}
    [read] = [
        m
        for m in exporter["volumeMounts"]
        if m["name"] == mounts["/ghcr-mirror-textfile"]
    ]
    assert f"--collector.textfile.directory={read['mountPath']}" in exporter["args"]
    probe = volumes[mounts["/ghcr-mirror-probe"]]["configMap"]
    assert probe["optional"] is True

    [config_map] = [
        d
        for d in yaml_fast.safe_load_all(
            rendered_k8s_text("nut", "ghcr-mirror-probe-configmap.yaml.j2")
        )
        if d
    ]
    [nut] = [
        d
        for d in yaml_fast.safe_load_all(rendered_k8s_text("nut", "deployment.yaml.j2"))
        if d
    ]
    assert config_map["metadata"]["name"] == probe["name"]
    image = nut["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image.endswith(f"/nut:{config_map['data']['tag']}")
