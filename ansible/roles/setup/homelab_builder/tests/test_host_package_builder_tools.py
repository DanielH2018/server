"""The builder's `homelab-tools` step: fetch, verify, cache, place, and build once per architecture.

H3.11. The render carries pins and no bytes; `Builder.package` copies each architecture's tree,
downloads and verifies the binaries into that copy, and runs nfpm there. A digest that differs
from its pin builds nothing and reads `reason="tools"`. Downloads come from a fake `urlopen`
keyed by URL, and nfpm is a shell stub that records what sat under `root/` when it ran.

Run: uv run pytest ansible/roles/setup/homelab_builder/tests/test_host_package_builder_tools.py
"""

import hashlib
import io
import json
import stat
import tarfile
import urllib.error
from pathlib import Path

import pytest

import homelab_tool_fetch as tf
import host_package_builder as hb
from lib import proc_testing
from renderer.renderer_lib import host_tools
from test_host_package_builder import (
    GREEN,
    KEYRING,
    FakeGitHub,
    Origin,
    _Response,
    config,
    metric,
)

SOPS = b"#!sops\n"
CHEZMOI = b"#!chezmoi\n"
URLS = {
    ("sops", "amd64"): "https://example.invalid/sops-amd64",
    ("sops", "arm64"): "https://example.invalid/sops-arm64",
    ("chezmoi", "amd64"): "https://example.invalid/chezmoi-amd64.tar.gz",
    ("chezmoi", "arm64"): "https://example.invalid/chezmoi-arm64.tar.gz",
}


def tarball(
    member: str = "chezmoi", data: bytes = CHEZMOI, kind: bytes = tarfile.REGTYPE
):
    """A .tar.gz holding one entry: a regular file, or a symlink when `kind` says so."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo(member)
        info.type = kind
        if kind == tarfile.REGTYPE:
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        else:
            info.linkname = "/etc/passwd"
            archive.addfile(info)
    return buffer.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Net(FakeGitHub):
    """`urlopen` for the check-runs API and for the tool downloads."""

    def __init__(self, files: dict[str, bytes]):
        super().__init__()
        self.files = files

    def __call__(self, request, timeout=None):
        if request.full_url in self.files:
            return _Response(self.files[request.full_url])
        if "/commits/" not in request.full_url:
            raise urllib.error.URLError("404")
        return super().__call__(request, timeout)


def good_files() -> dict[str, bytes]:
    return {
        URLS["sops", "amd64"]: SOPS + b"amd64",
        URLS["sops", "arm64"]: SOPS + b"arm64",
        URLS["chezmoi", "amd64"]: tarball(data=CHEZMOI + b"amd64"),
        URLS["chezmoi", "arm64"]: tarball(data=CHEZMOI + b"arm64"),
    }


def pins_for(files: dict[str, bytes], arch: str) -> dict:
    pins = {}
    for tool in ("sops", "chezmoi"):
        url = URLS[tool, arch]
        pins[tool] = {"url": url, "sha256": sha(files[url]), "version": "v1.0.0"}
        if tool == "chezmoi":
            pins[tool]["member"] = "chezmoi"
    return pins


# A stand-in for nfpm. It records the arch and the digest of each file under
# root/usr/lib/homelab/bin, then writes the .deb the way nfpm names one.
NFPM = """#!/bin/sh
set -e
arch=$(sed -n 's/^arch: //p' nfpm.yaml)
while [ $# -gt 0 ]; do
  case $1 in --target) target=$2 ;; esac
  shift
done
: > "${target}homelab-tools_${HOMELAB_VERSION}_$arch.deb"
for f in root/usr/lib/homelab/bin/*; do
  printf '%s %s %s\\n' "$arch" "$(basename "$f")" "$(sha256sum "$f" | cut -d' ' -f1)" >> "$TOOLS_RECORD"
done
"""


class ToolsBuilder(hb.Builder):
    """The builder with a render that yields `homelab-tools` for both architectures."""

    pinned: dict[str, bytes]

    def render(self, tree: Path, out: Path) -> None:
        for arch in ("amd64", "arm64"):
            directory = out / "packages" / "homelab-tools" / arch
            directory.mkdir(parents=True)
            (directory / "nfpm.yaml").write_text(f"name: homelab-tools\narch: {arch}\n")
            pins = pins_for(self.pinned, arch)
            (directory / tf.PINS_FILE).write_text(json.dumps(pins))
        target = out / hb.KEYRING
        target.parent.mkdir(parents=True)
        target.write_bytes(KEYRING.read_bytes())


@pytest.fixture
def lab(tmp_path, monkeypatch):
    origin = Origin(tmp_path / "origin")
    state = tmp_path / "state"
    (state / "staged").mkdir(parents=True)
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "archive-key.fpr").write_text("83B57416B342DDE1CBA0752D435D139ADFD6F016\n")
    nfpm = tmp_path / "nfpm"
    proc_testing.write_exec(nfpm, NFPM)
    record = tmp_path / "record"
    monkeypatch.setenv("TOOLS_RECORD", str(record))
    return origin, state, etc, nfpm, record


def run_builder(lab, net: Net, pinned: dict[str, bytes]) -> int:
    """One builder run whose render pins the digests of `pinned`, fetching through `net`."""
    origin, state, etc, nfpm, _ = lab
    builder = ToolsBuilder(
        config_path=config(etc),
        fingerprint_path=etc / "archive-key.fpr",
        hold_path=etc / "hold",
        state_dir=state,
        clone_url=str(origin.path),
        nfpm=str(nfpm),
        opener=net,
    )
    builder.pinned = pinned
    return builder.main()


def test_pinned_tools_are_built_once_per_architecture_from_a_scratch_copy(lab):
    origin, state, _, _, record = lab
    files = good_files()
    net = Net(files)
    net.runs[origin.commit("a")] = GREEN
    assert run_builder(lab, net, files) == 0
    debs = sorted(p.name for p in (state / "staged").glob("*.deb"))
    assert [d.split("_")[2] for d in debs] == ["amd64.deb", "arm64.deb"]
    seen = {tuple(line.split()) for line in record.read_text().splitlines()}
    assert seen == {
        ("amd64", "sops", sha(SOPS + b"amd64")),
        ("amd64", "chezmoi", sha(CHEZMOI + b"amd64")),
        ("arm64", "sops", sha(SOPS + b"arm64")),
        ("arm64", "chezmoi", sha(CHEZMOI + b"arm64")),
    }
    # The binaries sit in the scratch copy only, so publish-on-change compares pins.
    assert not list((state / "published").rglob("sops"))
    assert metric(state)[0] == "ok"


def test_a_binary_whose_digest_differs_from_its_pin_builds_nothing_and_reads_tools(lab):
    # fact: hosts/components/builder/CLAUDE.md#The `homelab-tools` step (H3.11)
    origin, state, _, _, record = lab
    pinned = good_files()
    served = {**pinned, URLS["sops", "arm64"]: b"tampered"}
    net = Net(served)
    net.runs[origin.commit("a")] = GREEN
    assert run_builder(lab, net, pinned) == 1
    assert run_builder(lab, net, pinned) == 0
    assert metric(state)[0] == "tools"
    assert list((state / "staged").iterdir()) == []
    assert not record.exists()
    assert not (state / tf.TOOLS_CACHE / sha(b"tampered")).exists()


def test_a_download_that_fails_reads_tools(lab):
    origin, state, _, _, _ = lab
    pinned = good_files()
    net = Net({k: v for k, v in pinned.items() if k != URLS["chezmoi", "amd64"]})
    net.runs[origin.commit("a")] = GREEN
    assert run_builder(lab, net, pinned) == 1
    assert metric(state)[0] == "tools"
    assert list((state / "staged").iterdir()) == []


def test_the_pin_file_name_and_bin_dir_equal_the_renderers():
    assert tf.PINS_FILE == host_tools.PINS_FILE
    assert tf.TOOLS_BIN == f"root{host_tools.BIN_DIR}"


# ── download_tool, tool_bytes and install_tools, each as an accept/reject pair ───────────


class fake:
    """`urlopen` answering every request with `body`, recording the URLs asked."""

    def __init__(self, body: bytes):
        self.body = body
        self.calls: list[str] = []

    def __call__(self, request, timeout=None):
        self.calls.append(request.full_url)
        return _Response(self.body)


def pin(body: bytes, **extra) -> dict:
    return {"url": "https://example.invalid/x", "sha256": sha(body), **extra}


def test_a_download_matching_its_pin_is_cached_under_its_digest_and_reused(tmp_path):
    body = b"binary"
    opener = fake(body)
    first = tf.download_tool(pin(body), tmp_path / "cache", opener)
    second = tf.download_tool(pin(body), tmp_path / "cache", opener)
    assert first == second == tmp_path / "cache" / sha(body)
    assert opener.calls == ["https://example.invalid/x"]


def test_a_corrupted_cache_entry_is_fetched_again(tmp_path):
    body = b"binary"
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / sha(body)).write_bytes(b"swapped")
    opener = fake(body)
    assert tf.download_tool(pin(body), cache, opener).read_bytes() == body
    assert opener.calls == ["https://example.invalid/x"]


def test_a_download_differing_from_its_pin_is_refused_and_not_cached(tmp_path):
    cache = tmp_path / "cache"
    with pytest.raises(tf.Failure) as caught:
        tf.download_tool(pin(b"expected"), cache, fake(b"other"))
    assert caught.value.reason == "tools"
    assert list(cache.iterdir()) == []


@pytest.mark.parametrize(
    "bad", [{"sha256": "abc"}, {"url": "http://example.invalid/x"}, {"url": None}]
)
def test_a_malformed_pin_is_refused_before_any_request(tmp_path, bad):
    opener = fake(b"x")
    with pytest.raises(tf.Failure):
        tf.download_tool({**pin(b"x"), **bad}, tmp_path, opener)
    assert opener.calls == []


def test_a_tar_member_is_read_by_exact_name(tmp_path):
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(tarball(data=b"payload"))
    assert tf.tool_bytes(archive, {"member": "chezmoi"}) == b"payload"


def test_a_member_one_directory_down_is_read(tmp_path):
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(tarball(member="uv-x86_64-unknown-linux-gnu/uv", data=b"uv"))
    assert tf.tool_bytes(archive, {"member": "uv-x86_64-unknown-linux-gnu/uv"}) == b"uv"


@pytest.mark.parametrize(
    ("member", "kind"),
    [
        ("absent", tarfile.REGTYPE),
        ("chezmoi", tarfile.SYMTYPE),
        ("../chezmoi", tarfile.REGTYPE),
        ("/etc/passwd", tarfile.REGTYPE),
        ("a/b/c", tarfile.REGTYPE),
        ("a/../chezmoi", tarfile.REGTYPE),
        ("a//chezmoi", tarfile.REGTYPE),
    ],
)
def test_a_missing_linked_or_path_shaped_member_is_refused(tmp_path, member, kind):
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(tarball(member="chezmoi", kind=kind))
    with pytest.raises(tf.Failure) as caught:
        tf.tool_bytes(archive, {"member": member})
    assert caught.value.reason == "tools"


def test_a_pin_without_a_member_installs_the_download_itself(tmp_path):
    raw = tmp_path / "raw"
    raw.write_bytes(b"raw")
    assert tf.tool_bytes(raw, {}) == b"raw"


def test_installed_tools_are_executable_and_a_bad_pin_installs_none(tmp_path):
    good = {"sops": pin(b"sops")}
    directory = tmp_path / "tree"
    directory.mkdir()
    (directory / tf.PINS_FILE).write_text(json.dumps(good))
    tf.install_tools(directory, tmp_path / "cache", fake(b"sops"))
    mode = (directory / tf.TOOLS_BIN / "sops").stat().st_mode
    assert mode & stat.S_IXUSR

    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / tf.PINS_FILE).write_text(json.dumps({**good, "chezmoi": pin(b"want")}))
    with pytest.raises(tf.Failure):
        tf.install_tools(bad, tmp_path / "cache2", fake(b"sops"))
    assert not (bad / tf.TOOLS_BIN).exists()


def test_the_cache_keeps_the_pinned_digests_and_drops_the_rest(tmp_path):
    cache = tmp_path / tf.TOOLS_CACHE
    cache.mkdir()
    (cache / "keep").write_text("")
    (cache / "old").write_text("")
    render = tmp_path / "render" / "packages" / "homelab-tools" / "amd64"
    render.mkdir(parents=True)
    (render / tf.PINS_FILE).write_text(json.dumps({"sops": {"sha256": "keep"}}))
    tf.prune_tools_cache(cache, tmp_path / "render")
    assert [p.name for p in cache.iterdir()] == ["keep"]
