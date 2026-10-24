"""Fetch, verify and place the pinned binaries of a rendered `homelab-tools` tree.

Split from host_package_builder.py (H3.11), which imports it and re-exports these names. The
builder runs it on daniel-box, and CI's `host_packages` job runs the same `install_tools`
through `scripts/validate/host_packages.py fetch-tools`, so a pin that does not verify fails in
CI before it reaches master. Stdlib only, because the builder ships beside it with nothing else.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tarfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any


class Failure(Exception):
    """A run that ends with a reason, before it builds."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


# A rendered `homelab-tools` tree carries its pins in this file beside `nfpm.yaml`, and the
# package installs each binary under `TOOLS_BIN` in its `root/`. A test holds both equal to
# scripts/renderer/renderer_lib/host_tools.py, which this script cannot import.
PINS_FILE = "tools.json"
TOOLS_BIN = "root/usr/lib/homelab/bin"
TOOLS_CACHE = "tools-cache"
# A download, or a file inside one, larger than this fails the build. The largest pin is
# about 60 MB.
TOOL_MAX_BYTES = 256 * 1024 * 1024
TOOL_TIMEOUT_S = 120
_TOOL_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")
# One optional directory segment: uv ships `uv-x86_64-unknown-linux-gnu/uv`.
_MEMBER = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9._-]*/)?[A-Za-z0-9][A-Za-z0-9._-]*")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_tool(
    pin: dict, cache: Path, opener: Callable[..., Any] | None = None
) -> Path:
    """The file `pin` names, verified against its sha256 and cached at `cache/<sha256>`.

    A cached file is checked again on every use, so a corrupted or swapped cache entry is
    fetched anew instead of installed. A download whose digest differs from the pin is deleted
    and never cached.

    Raises:
        Failure: reason `tools`, because the pin is malformed, the download failed, it exceeds
            `TOOL_MAX_BYTES`, or its digest differs from the pin's.
    """
    want, url = pin.get("sha256"), pin.get("url")
    if not isinstance(want, str) or not _SHA256.fullmatch(want):
        raise Failure("tools", f"pin sha256 {want!r} is not 64 lowercase hex digits")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise Failure("tools", f"pin url {url!r} is not an https URL")
    path = cache / want
    if path.is_file() and file_sha256(path) == want:
        return path
    cache.mkdir(parents=True, exist_ok=True)
    partial = cache / f"{want}.part"
    digest, size = hashlib.sha256(), 0
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "homelab-builder"})
        with (
            (opener or urllib.request.urlopen)(request, timeout=TOOL_TIMEOUT_S) as resp,
            partial.open("wb") as out,
        ):
            for chunk in iter(lambda: resp.read(1 << 20), b""):
                size += len(chunk)
                if size > TOOL_MAX_BYTES:
                    raise Failure("tools", f"{url} exceeds {TOOL_MAX_BYTES} bytes")
                digest.update(chunk)
                out.write(chunk)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        partial.unlink(missing_ok=True)
        raise Failure("tools", f"cannot fetch {url}: {exc}") from exc
    except Failure:
        partial.unlink(missing_ok=True)
        raise
    if digest.hexdigest() != want:
        partial.unlink(missing_ok=True)
        raise Failure(
            "tools",
            f"{url} has sha256 {digest.hexdigest()}, the pin names {want}; nothing built",
        )
    os.replace(partial, path)
    return path


def tool_bytes(download: Path, pin: dict) -> bytes:
    """The binary to install: the download itself, or the pin's `member` of a `.tar.gz`.

    Only that member is read, by its exact name, and only a regular file qualifies, so no path
    in the archive is ever resolved or written.

    Raises:
        Failure: reason `tools`, because the member is missing, is not a regular file, or
            exceeds `TOOL_MAX_BYTES`.
    """
    member = pin.get("member")
    if member is None:
        return download.read_bytes()
    if not isinstance(member, str) or not _MEMBER.fullmatch(member):
        raise Failure("tools", f"pin member {member!r} is not a plain file name")
    try:
        with tarfile.open(download, "r:gz") as archive:
            info = archive.getmember(member)
            handle = archive.extractfile(info) if info.isreg() else None
            if handle is None or info.size > TOOL_MAX_BYTES:
                raise Failure(
                    "tools", f"{download.name}: {member} is not a usable file"
                )
            return handle.read()
    except (KeyError, tarfile.TarError, OSError) as exc:
        raise Failure("tools", f"{download.name}: cannot read {member}: {exc}") from exc


def install_tools(
    directory: Path, cache: Path, opener: Callable[..., Any] | None = None
) -> None:
    """Fetch, verify and place every tool `directory/tools.json` pins under its `root/`.

    Every tool is verified before the first is written, so a mismatch leaves `directory`
    without any binary.

    Raises:
        Failure: reason `tools`, from `download_tool` or `tool_bytes`, or because `tools.json`
            is unreadable or names a tool that is not a plain name.
    """
    try:
        pins = json.loads((directory / PINS_FILE).read_text())
    except (OSError, ValueError) as exc:
        raise Failure("tools", f"cannot read {directory / PINS_FILE}: {exc}") from exc
    if not isinstance(pins, dict) or not pins:
        raise Failure("tools", f"{directory / PINS_FILE} holds no tool")
    binaries: dict[str, bytes] = {}
    for tool, pin in sorted(pins.items()):
        if not _TOOL_NAME.fullmatch(tool) or not isinstance(pin, dict):
            raise Failure("tools", f"{directory / PINS_FILE}: {tool!r} is not a pin")
        binaries[tool] = tool_bytes(download_tool(pin, cache, opener), pin)
    target = directory / TOOLS_BIN
    target.mkdir(parents=True, exist_ok=True)
    for tool, data in binaries.items():
        (target / tool).write_bytes(data)
        (target / tool).chmod(0o755)


def fetch_tools(
    directory: Path, cache: Path, opener: Callable[..., Any] | None = None
) -> None:
    """Download and verify every tool `directory` pins; do nothing for a tree with none.

    Raises:
        Failure: reason `tools`, from `download_tool`, or because `tools.json` is unreadable.
    """
    if not (directory / PINS_FILE).is_file():
        return
    try:
        pins = json.loads((directory / PINS_FILE).read_text())
    except (OSError, ValueError) as exc:
        raise Failure("tools", f"cannot read {directory / PINS_FILE}: {exc}") from exc
    for pin in pins.values() if isinstance(pins, dict) else ():
        download_tool(pin, cache, opener)


def prune_tools_cache(cache: Path, render: Path) -> None:
    """Delete each cached download that no pin under `render` names.

    A bumped pin would otherwise leave its old download in the cache for good.
    """
    keep = {
        pin["sha256"]
        for pins in render.rglob(PINS_FILE)
        for pin in json.loads(pins.read_text()).values()
    }
    for entry in cache.glob("*"):
        if entry.name not in keep:
            entry.unlink(missing_ok=True)


def fetch_all(
    directories: list[Path],
    state_dir: Path,
    render: Path,
    opener: Callable[..., Any] | None = None,
) -> None:
    """Verify every pin under `directories`, then drop the cache entries no pin of `render` names."""
    cache = state_dir / TOOLS_CACHE
    for directory in directories:
        fetch_tools(directory, cache, opener)
    if cache.is_dir():
        prune_tools_cache(cache, render)


def scratch_tree(
    directory: Path,
    work: Path,
    state_dir: Path,
    opener: Callable[..., Any] | None = None,
) -> Path:
    """A copy of the rendered tree `directory` under `work/`, with its binaries placed.

    The binaries sit in the copy only, so the render and `published/` never carry them and
    publish-on-change keeps comparing pins.
    """
    build = work / "tools" / f"{directory.parent.name}-{directory.name}"
    shutil.rmtree(build, ignore_errors=True)
    shutil.copytree(directory, build, symlinks=True)
    install_tools(build, state_dir / TOOLS_CACHE, opener)
    return build
