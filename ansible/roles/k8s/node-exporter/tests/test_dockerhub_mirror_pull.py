"""The dockerhub-mirror-pull sidecar reports whether a node's docker.io mirror serves pulls (#6535).

It runs ghcr-mirror-pull.sh with CHECK, REPOSITORY and REFERENCE set, so it GETs the exporter's
own image by digest through the node's docker.io mirror endpoint. These tests run the script
once under `sh` against a local HTTP server standing in for the mirror, with the host's wget,
and read the rendered DaemonSet for the wiring.

Run: uv run pytest ansible/roles/k8s/node-exporter/tests/test_dockerhub_mirror_pull.py
"""

import re
import shutil
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from _helpers import K8S_ROLES, load_defaults
from _k8s_render import rendered_k8s_text
from lib import yaml_fast
from lib.proc_testing import run

ROLE = K8S_ROLES / "node-exporter"
SCRIPT = ROLE / "files" / "ghcr-mirror-pull.sh"
RULES = K8S_ROLES / "observability" / "files" / "rules" / "registry.yaml"
DIGEST = "sha256:" + "1" * 64
SERVED = f"/v2/prom/node-exporter/manifests/{DIGEST}"
DEAD = "http://127.0.0.1:9"
CHECK = "dockerhub-mirror-pull"
SUCCESS = f'homelab_check_success{{check="{CHECK}"}}'


class _Mirror(BaseHTTPRequestHandler):
    """Answers 200 for `SERVED` when the request accepts an OCI index, 404 otherwise."""

    def do_GET(self) -> None:
        accept = self.headers.get("Accept", "")
        ok = self.path == SERVED and "application/vnd.oci.image.index.v1+json" in accept
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


def _write_once(
    tmp_path: Path, node: str, local: str, remote: str, reference: str = DIGEST
) -> dict[str, str]:
    """Run the script once as this sidecar on `node`, with daniel-box as the registry node."""
    bin_dir, out = tmp_path / "bin", tmp_path / "out"
    bin_dir.mkdir(exist_ok=True)
    out.mkdir(exist_ok=True)
    if not (bin_dir / "wget").exists():
        real_wget = shutil.which("wget", path="/usr/bin:/bin")
        assert real_wget is not None, "these tests need the host's wget"
        (bin_dir / "wget").symlink_to(real_wget)
    env = {
        "CHECK": CHECK,
        "REPOSITORY": "prom/node-exporter",
        "REFERENCE": reference,
        # A directory with no files: the environment's reference must win over it.
        "TARGET_DIR": str(tmp_path / "absent"),
        "NODE_NAME": node,
        "REGISTRY_NODE": "daniel-box",
        "LOCAL_ENDPOINT": local,
        "REMOTE_ENDPOINT": remote,
        "TEXTFILE_DIR": str(out),
        "TIMEOUT_S": "5",
        "ONCE": "1",
        "PATH": f"{bin_dir}:/usr/bin:/bin",
    }
    result = run(["sh", str(SCRIPT)], env=env, check=False)
    assert result.returncode == 0, result.stderr
    assert [p.name for p in out.iterdir()] == [f"{CHECK}.prom"]
    text = (out / f"{CHECK}.prom").read_text()
    return dict(
        line.rsplit(" ", 1) for line in text.splitlines() if not line.startswith("#")
    )


def test_a_served_digest_keeps_the_check_green_on_each_side_of_the_node_split(
    tmp_path, mirror
):
    assert _write_once(tmp_path, "daniel-box", mirror, DEAD)[SUCCESS] == "1"
    assert _write_once(tmp_path, "daniel-server", DEAD, mirror)[SUCCESS] == "1"


def test_a_refused_endpoint_turns_the_check_red(tmp_path, mirror):
    assert _write_once(tmp_path, "daniel-server", mirror, DEAD)[SUCCESS] == "0"


def test_a_digest_the_mirror_does_not_serve_turns_the_check_red(tmp_path, mirror):
    other = "sha256:" + "2" * 64
    assert _write_once(tmp_path, "daniel-box", mirror, DEAD, other)[SUCCESS] == "0"


def test_both_not_reporting_rules_carry_the_scripts_deadline(tmp_path, mirror):
    deadline = _write_once(tmp_path, "daniel-box", mirror, DEAD)[
        f'homelab_check_deadline_seconds{{check="{CHECK}"}}'
    ]
    [group] = yaml_fast.safe_load(RULES.read_text())["groups"]
    rules = [
        r for r in group["rules"] if r.get("alert") == "DockerhubMirrorPullNotReporting"
    ]
    assert len(rules) == 2
    for rule in rules:
        assert re.findall(r"> (\d+)", rule["expr"]) == [deadline]
        assert re.findall(r"\[(\d+)s\]", rule["expr"]) == [deadline]


def test_the_sidecar_gets_the_exporters_own_digest_through_the_docker_io_port():
    [ds] = [
        d
        for d in yaml_fast.safe_load_all(
            rendered_k8s_text("node-exporter", "daemonset.yaml.j2")
        )
        if d
    ]
    pod = ds["spec"]["template"]["spec"]
    containers = {c["name"]: c for c in pod["containers"]}
    sidecar, exporter = containers[CHECK], containers["node-exporter"]
    assert sidecar["command"][-1].strip() == SCRIPT.read_text().strip()
    env = {e["name"]: e.get("value") for e in sidecar["env"]}
    image = load_defaults(ROLE)["node_exporter_k8s_image"]
    repository, digest = image.split("@")
    assert env["CHECK"] == CHECK
    assert env["REPOSITORY"] == repository.rsplit(":", 1)[0]
    assert env["REFERENCE"] == digest
    assert env["LOCAL_ENDPOINT"].endswith(":5005"), env
    assert env["REMOTE_ENDPOINT"].endswith(":5005"), env
    [mount] = sidecar["volumeMounts"]
    assert env["TEXTFILE_DIR"] == mount["mountPath"]
    [read] = [m for m in exporter["volumeMounts"] if m["name"] == mount["name"]]
    assert f"--collector.textfile.directory={read['mountPath']}" in exporter["args"]
