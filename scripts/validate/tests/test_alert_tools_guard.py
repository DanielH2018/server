"""`scripts/validate/alert_tools.sh` unpacks only a verified tarball, and trusts only a stamp.

`curl` is faked on PATH, ahead of leakguard's recording shim, so no test reaches GitHub. The
fake serves fixture tarballs and sums files from a temporary directory, keyed by the project
and file name in the URL, and logs every fetch. That log is what tells "reused the cache"
apart from "fetched it again".

Each case runs the real script from a throwaway git repository whose observability defaults
pin both images. The script puts its cache under the git common dir, so every run gets
`scrubbed_env()`: an inherited `GIT_DIR` from a commit hook would otherwise point it at the
real clone's `.git/alert-tools/`.

Run: uv run pytest scripts/validate/tests/test_alert_tools_guard.py
"""

import hashlib
import io
import platform
import shutil
import tarfile
from pathlib import Path

import pytest

from lib.git_testing import commit, git, init_repo, scrubbed_env
from lib.proc_testing import fake_bin, run
from lib.repo_paths import REPO

SCRIPT = REPO / "scripts" / "validate" / "alert_tools.sh"
ARCH = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}[platform.machine()]
VERSIONS = {"prometheus": ("3.15.0", "promtool"), "alertmanager": ("0.34.1", "amtool")}

DEFAULTS = """\
observability_k8s_prometheus_image: prom/prometheus:v3.15.0@sha256:{0}
observability_k8s_alertmanager_image: prom/alertmanager:v0.34.1@sha256:{0}
""".format("0" * 64)

FAKE_CURL = """#!/usr/bin/env bash
set -euo pipefail
out="" url=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
rel="${url#https://github.com/prometheus/}"
project="${rel%%/*}"
printf '%s\\n' "$url" >>"$CURL_LOG"
cp "$RELEASES/$project/${url##*/}" "$out"
"""


def _tarball(project: str, version: str, binary: str) -> bytes:
    """A release tarball shaped like upstream's: `<project>-<version>.linux-<arch>/<binary>`."""
    body = f"#!/bin/sh\necho {binary} $*\n".encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(f"{project}-{version}.linux-{ARCH}/{binary}")
        info.size = len(body)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


@pytest.fixture
def checkout(tmp_path):
    """A throwaway git repo holding the real script and pinned defaults, with releases served.

    The repository is `tmp_path / "main"`. The served releases, the fake `curl` and its log sit
    beside it in `tmp_path`, outside every worktree.
    """
    repo = init_repo(tmp_path / "main")
    (repo / "scripts" / "validate").mkdir(parents=True)
    shutil.copy2(SCRIPT, repo / "scripts" / "validate" / "alert_tools.sh")
    commit(
        repo,
        "pin the images",
        **{"ansible/roles/k8s/observability/defaults/main.yml": DEFAULTS},
    )
    for project, (version, binary) in VERSIONS.items():
        served = tmp_path / "releases" / project
        served.mkdir(parents=True)
        name = f"{project}-{version}.linux-{ARCH}.tar.gz"
        data = _tarball(project, version, binary)
        (served / name).write_bytes(data)
        (served / "sha256sums.txt").write_text(
            f"{hashlib.sha256(data).hexdigest()}  {name}\n"
        )
    fake_bin(tmp_path / "bin", curl=FAKE_CURL)
    return repo


def _run(repo: Path, *args: str):
    env = scrubbed_env(
        CURL_LOG=str(repo.parent / "curl.log"), RELEASES=str(repo.parent / "releases")
    )
    return run(
        [str(repo / "scripts" / "validate" / "alert_tools.sh"), *args],
        cwd=repo,
        env=env,
        stub_bin=repo.parent / "bin",
    )


def _fetches(repo: Path) -> list[str]:
    log = repo.parent / "curl.log"
    return log.read_text().splitlines() if log.exists() else []


def _tool_dir(repo: Path, project: str) -> Path:
    return repo / ".git" / "alert-tools" / f"{project}-{VERSIONS[project][0]}"


def test_a_tarball_whose_digest_is_not_in_the_sums_file_is_flagged(checkout):
    sums = checkout.parent / "releases" / "prometheus" / "sha256sums.txt"
    sums.write_text(f"{'f' * 64}  prometheus-3.15.0.linux-{ARCH}.tar.gz\n")

    result = _run(checkout)

    assert result.returncode != 0
    assert "sha256sums.txt lists" in result.stderr, result.stderr
    assert not (_tool_dir(checkout, "prometheus") / ".provisioned").exists()
    assert not (_tool_dir(checkout, "prometheus") / "promtool").exists()


def test_a_version_directory_with_files_but_no_stamp_is_flagged_and_fetched_again(
    checkout,
):
    partial = _tool_dir(checkout, "prometheus")
    partial.mkdir(parents=True)
    (partial / "leftover").write_text("half an unpack\n")

    result = _run(checkout, "promtool", "--version")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "promtool --version"
    assert any("/prometheus/releases/" in url for url in _fetches(checkout)), _fetches(
        checkout
    )
    assert not (partial / "leftover").exists()
    assert (partial / ".provisioned").exists()


def test_a_complete_stamped_cache_is_reused_with_no_fetch_is_clean(checkout):
    assert _run(checkout).returncode == 0
    (checkout.parent / "curl.log").unlink()

    result = _run(checkout, "amtool", "check-config", "x.yml")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "amtool check-config x.yml"
    assert _fetches(checkout) == []


def test_a_second_worktree_of_the_clone_reuses_the_first_ones_cache_is_clean(checkout):
    assert _run(checkout).returncode == 0
    (checkout.parent / "curl.log").unlink()
    second = checkout.parent / "second"
    git(checkout, "worktree", "add", "-q", str(second))

    result = _run(second, "promtool", "--version")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "promtool --version"
    assert _fetches(second) == []
    assert not (second / ".cache").exists()
