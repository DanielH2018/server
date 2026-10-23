"""The `tools:` key of a shared package spec: pinned upstream binaries, built once per architecture.

`homelab-tools` (H3, C5, *`homelab-tools`*) ships upstream release binaries such as `sops` and
`chezmoi`. A render reads the git tree alone, so it carries the pins and never the bytes: the
builder downloads each binary, checks its sha256 against the pin, and puts it at the path this
module derives. The spec lists, for each tool and each architecture::

    tools:
      sops:
        amd64:
          # renovate: datasource=github-release-attachments depName=getsops/sops
          version: v3.13.3
          sha256: <64 hex digits of the file the url names>
          url: https://github.com/getsops/sops/releases/download/{version}/sops-{version}.linux.amd64
        arm64: {...}
      chezmoi:
        amd64:
          ...
          member: chezmoi          # the url is a .tar.gz; this is the file inside it to install

Each architecture block carries its own `version` and annotation, because the Renovate manager
moves one version and one digest per match. A tool's versions must agree across architectures.
`{version}` in a `url` is the pin as written (`v3.13.3`); `{bare}` is the pin without its `v`.

A binary installs at `BIN_DIR/<tool>`. The spec cannot name another path, so a tool never lands
on `PATH`. The renderer writes one tree per architecture, `<package>/<arch>/`, each with a
literal `arch:` in its `nfpm.yaml`, so nfpm runs once per architecture.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import re
from dataclasses import dataclass

from renderer.renderer_lib.cluster import RenderRefused

ARCHES = ("amd64", "arm64")
BIN_DIR = "/usr/lib/homelab/bin"
# The file a render writes beside `nfpm.yaml`, listing the pins of that architecture's tools.
PINS_FILE = "tools.json"

_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
_VERSION = re.compile(r"v?\d+\.\d+\.\d+")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MEMBER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_ENTRY_KEYS = frozenset({"version", "url", "sha256", "member"})


@dataclass(frozen=True)
class Pin:
    """One tool for one architecture: what to fetch, and the digest it must have.

    Attributes:
        version: The pin as written, `v3.13.3` or `2.73.1`.
        url: The upstream URL, with `{version}` and `{bare}` expanded.
        sha256: The digest of the file the URL serves.
        member: The file to install from inside a `.tar.gz` download, or None when the
            download is the binary.
    """

    version: str
    url: str
    sha256: str
    member: str | None = None

    def as_json(self) -> dict[str, str]:
        entry = {"version": self.version, "url": self.url, "sha256": self.sha256}
        if self.member:
            entry["member"] = self.member
        return entry


def tool_path(tool: str) -> str:
    """Where `tool` installs: `BIN_DIR/<tool>`."""
    return f"{BIN_DIR}/{tool}"


def parse_tools(raw: object, where: str) -> dict[str, dict[str, Pin]]:
    """The `tools:` mapping of the spec at `where`, validated, as `{tool: {arch: Pin}}`.

    Raises:
        RenderRefused: A name is not plain, an architecture is missing or unknown, a version,
            digest, URL or member is malformed, or one tool's versions differ by architecture.
    """
    if not isinstance(raw, dict) or not raw:
        raise RenderRefused(f"{where}: tools is not a non-empty mapping")
    tools: dict[str, dict[str, Pin]] = {}
    for tool, arches in raw.items():
        if not isinstance(tool, str) or not _NAME.fullmatch(tool):
            raise RenderRefused(
                f"{where}: tool name {tool!r} is not a plain name "
                "(lowercase letters, digits, hyphens)"
            )
        if not isinstance(arches, dict) or set(arches) != set(ARCHES):
            raise RenderRefused(
                f"{where}: tool {tool} lists {sorted(arches) if isinstance(arches, dict) else arches!r}, "
                f"not exactly {list(ARCHES)}"
            )
        tools[tool] = {
            arch: _pin(f"{where}: {tool} {arch}", arches[arch]) for arch in ARCHES
        }
        if len({pin.version for pin in tools[tool].values()}) != 1:
            raise RenderRefused(
                f"{where}: tool {tool} pins different versions per architecture"
            )
    return tools


def _pin(where: str, entry: object) -> Pin:
    if not isinstance(entry, dict) or set(entry) - _ENTRY_KEYS:
        raise RenderRefused(f"{where}: an entry holds only {sorted(_ENTRY_KEYS)}")
    version, sha256, url = entry.get("version"), entry.get("sha256"), entry.get("url")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise RenderRefused(f"{where}: version {version!r} is not vX.Y.Z or X.Y.Z")
    if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
        raise RenderRefused(
            f"{where}: sha256 {sha256!r} is not 64 lowercase hex digits"
        )
    if not isinstance(url, str):
        raise RenderRefused(f"{where}: url {url!r} is not a string")
    expanded = url.replace("{version}", version).replace("{bare}", version.lstrip("v"))
    if not expanded.startswith("https://") or "{" in expanded or "}" in expanded:
        raise RenderRefused(
            f"{where}: url {url!r} must be https, and may hold only {{version}} and {{bare}}"
        )
    member = entry.get("member")
    if member is not None and (
        not isinstance(member, str) or not _MEMBER.fullmatch(member)
    ):
        raise RenderRefused(f"{where}: member {member!r} is not a plain file name")
    return Pin(version, expanded, sha256, member)
