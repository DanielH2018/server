"""homelab-takeover-check-flux installs the pinned flux CLI the takeover check runs, or takes it back.

The rendered script runs for real against a `file://` tarball under a temporary root
(`TAKEOVER_CHECK_FLUX_ROOT`), so no case downloads anything or writes outside `tmp_path`.
`curl` is an exported bash function that copies the `file://` source to `--output`; leakguard
stubs the real binary on PATH.

Run: uv run pytest hosts/lib/tests/test_takeover_check_flux.py
"""

import hashlib
import io
import tarfile
from pathlib import Path

from _shell_render import render_shell_script
from lib.proc_testing import run
from renderer.renderer_lib import host as host_mode

TEMPLATE = ("components", "claude-code", "takeover-check-flux.sh.j2")
LIB = "/usr/local/lib/homelab-takeover-check"
STATE = "/var/lib/homelab-takeover-check"
CURL_STUB = (
    'local out="" src=""; while [ $# -gt 0 ]; do case "$1" in '
    '--output) out="$2"; shift ;; file://*) src="${1#file://}" ;; esac; shift; done; '
    'cp "$src" "$out"'
)


def _tarball(tmp_path: Path, body: bytes) -> tuple[str, str]:
    """A release-shaped tarball holding `flux`; returns its file:// URL and sha256."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("flux")
        info.size = len(body)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(body))
    path = tmp_path / "flux_9.9.9_linux_amd64.tar.gz"
    path.write_bytes(buf.getvalue())
    return f"file://{path}", hashlib.sha256(buf.getvalue()).hexdigest()


def _run(tmp_path: Path, *, enabled: bool, url: str = "", sha256: str = ""):
    script = tmp_path / "takeover-check-flux"
    script.write_text(
        render_shell_script(
            *TEMPLATE,
            overrides={
                "claude_code_takeover_check_enabled": enabled,
                "claude_code_takeover_check_flux_url": url,
                "claude_code_takeover_check_flux_sha256": sha256,
            },
        )
    )
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    proc = run(
        ["bash", str(script)],
        env={
            "PATH": "/usr/bin:/bin",
            "TAKEOVER_CHECK_FLUX_ROOT": str(root),
            "BASH_FUNC_curl%%": f"() {{ {CURL_STUB}; }}",
        },
    )
    return proc, root


def test_a_tarball_at_the_pin_installs_flux_and_records_the_pin(tmp_path):
    url, sha256 = _tarball(tmp_path, b"#!/bin/sh\necho flux\n")
    proc, root = _run(tmp_path, enabled=True, url=url, sha256=sha256)
    assert proc.returncode == 0, proc
    flux = root / LIB.lstrip("/") / "flux"
    assert flux.read_bytes() == b"#!/bin/sh\necho flux\n"
    assert flux.stat().st_mode & 0o777 == 0o755
    assert (flux.parent / "flux.sha256").read_text().strip() == sha256


def test_a_tarball_off_the_pin_is_refused_and_installs_nothing(tmp_path):
    url, _ = _tarball(tmp_path, b"#!/bin/sh\necho tampered\n")
    proc, root = _run(tmp_path, enabled=True, url=url, sha256="0" * 64)
    assert proc.returncode != 0
    assert not (root / LIB.lstrip("/") / "flux").exists()


def test_a_copy_already_at_the_pin_downloads_nothing(tmp_path):
    url, sha256 = _tarball(tmp_path, b"#!/bin/sh\necho flux\n")
    _run(tmp_path, enabled=True, url=url, sha256=sha256)
    proc, root = _run(
        tmp_path, enabled=True, url="file:///nonexistent.tar.gz", sha256=sha256
    )
    assert proc.returncode == 0, proc
    assert (root / LIB.lstrip("/") / "flux").exists()


def test_switched_off_it_removes_the_copy_and_the_state_directory(tmp_path):
    root = tmp_path / "root"
    for directory in (LIB, STATE):
        (root / directory.lstrip("/")).mkdir(parents=True)
    (root / LIB.lstrip("/") / "flux").write_text("old")
    proc, _ = _run(tmp_path, enabled=False)
    assert proc.returncode == 0, proc
    assert not (root / LIB.lstrip("/")).exists()
    assert not (root / STATE.lstrip("/")).exists()


def test_the_oneshot_ships_on_both_hosts_and_reruns_when_its_script_changes():
    for host in ("daniel-box", "daniel-server"):
        package = host_mode.render_host(host)
        assert "/usr/lib/homelab/takeover-check-flux" in package.files, host
        assert (
            package.activations["/usr/lib/homelab/takeover-check-flux"]
            == "try-restart:homelab-takeover-check-flux.service"
        )
    box = host_mode.render_host("daniel-box").files
    # daniel-box arms the check, so its render installs rather than removes.
    script = box["/usr/lib/homelab/takeover-check-flux"][0].decode()
    assert "sha256sum --check" in script
    assert 'rm -rf "$lib_dir"' not in script
