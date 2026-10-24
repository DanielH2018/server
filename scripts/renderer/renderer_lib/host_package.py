"""The `Package` a host or shared package renders to, and the `nfpm.yaml` it writes.

`host.py` builds a `Package` from a spec and writes it; this module holds the value itself:
the files, links, scripts, dependencies and pinned tools a render collected, and the nfpm spec
that lists them. `host.py` re-exports `Package` and the constants below, which its callers and
tests read through `host_mode`.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from dataclasses import dataclass, field
from pathlib import PurePosixPath

import yaml

from renderer.renderer_lib.host_manifest import DROPIN, UNIT_DIR
from renderer.renderer_lib.host_tools import Pin, tool_path

# The builder exports the version (H3, *Package rules*). Kept out of the render, so a render
# changes only when the package's content does, and nfpm expands it at build time.
VERSION_FIELD = "${HOMELAB_VERSION}"

# Each maintainer script a spec may list, and the nfpm `scripts:` key that installs it. `preinst`
# refuses a hostname mismatch, so only a host package may list it.
MAINTAINER_SCRIPTS = {
    "preinst": "preinstall",
    "postinst": "postinstall",
    "prerm": "preremove",
}
UNIT_SUFFIXES = (".service", ".timer", ".socket", ".path")


@dataclass
class Package:
    """One rendered package: its name, its C1 directory, and every file it installs.

    Attributes:
        name: The Debian package name.
        directory: `hosts/<host>` or `packages/<pkg>`, relative to `--out`.
        files: Each installed path mapped to its bytes and its octal mode.
        links: Each installed symlink's path mapped to its target, relative to its directory.
        depends: The `Depends` entries, in the order the spec lists them.
        description: The package description.
        scripts: Each maintainer script's name, such as `postinst`, mapped to its bytes.
        policies: Each unit given a restart policy, mapped to `manual` or `windowed`.
        legacy: The legacy entries reconcile retires, each with its values rendered.
        tools: Each pinned upstream binary, by name and then architecture (`host_tools.py`).
            A package with tools renders one tree per architecture.
        secrets: Whether the package lists the `secrets-unit` component (`host_outputs.py`).
        credentials: The secret keys `homelab-secrets` writes to `/run/credstore`.
        outputs: Each secret-bearing output, in the shape `secrets.json` carries.
        secrets_inputs_sha256: The hash reconcile compares, set only when `secrets-unit` is.
        activations: Each file whose change reconcile activates, mapped to the action.
        ufw_rules: The ufw rules the package declares, each with its values rendered.
        ufw_legacy: The Ansible-era ufw rules reconcile deletes once replaced.
    """

    name: str
    directory: str
    files: dict[str, tuple[bytes, int]] = field(default_factory=dict)
    links: dict[str, str] = field(default_factory=dict)
    depends: list[str] = field(default_factory=list)
    description: str = ""
    scripts: dict[str, bytes] = field(default_factory=dict)
    policies: dict[str, str] = field(default_factory=dict)
    legacy: list[dict[str, str]] = field(default_factory=list)
    tools: dict[str, dict[str, Pin]] = field(default_factory=dict)
    secrets: bool = False
    credentials: list[str] = field(default_factory=list)
    outputs: list[dict] = field(default_factory=list)
    secrets_inputs_sha256: str | None = None
    activations: dict[str, str] = field(default_factory=dict)
    ufw_rules: list[dict] = field(default_factory=list)
    ufw_legacy: list[dict] = field(default_factory=list)

    def installed_paths(self) -> list[str]:
        """Every path the package installs: its files and the path of each pinned tool."""
        return sorted({*self.files, *(tool_path(tool) for tool in self.tools)})

    def units(self) -> list[str]:
        """The unit files the package installs under `/usr/lib/systemd/system/`, by name.

        A drop-in and an enable link sit one directory deeper, so neither is listed.
        """
        return sorted(
            PurePosixPath(dst).name
            for dst in self.files
            if PurePosixPath(dst).parent == UNIT_DIR and dst.endswith(UNIT_SUFFIXES)
        )

    def dropin_targets(self) -> list[str]:
        """The units, of any package, that this package installs a homelab drop-in for."""
        return sorted(
            path.parent.name.removesuffix(".d")
            for path in map(PurePosixPath, self.files)
            if path.parent.parent == UNIT_DIR and path.name == DROPIN
        )

    def enable_links(self) -> list[str]:
        """The units whose enable link, under `<target>.wants/`, the package installs.

        A secret-bearing output's symlink (`host_outputs.py`) is a link too, and no unit.
        """
        return sorted(
            {
                PurePosixPath(dst).name
                for dst in self.links
                if PurePosixPath(dst).parent.parent == UNIT_DIR
                and PurePosixPath(dst).parent.name.endswith(".wants")
            }
        )

    def nfpm_spec(self, arch: str = "all") -> str:
        """The `nfpm.yaml` the builder runs `nfpm package` on, from the package directory.

        Args:
            arch: The Debian architecture. A package with tools passes each of `ARCHES`.
        """
        contents = {
            dst: {"src": f"root{dst}", "dst": dst, "file_info": {"mode": mode}}
            for dst, (_, mode) in self.files.items()
        }
        # The binary is absent from the render. The builder puts it at `src` before nfpm runs.
        contents |= {
            tool_path(tool): {
                "src": f"root{tool_path(tool)}",
                "dst": tool_path(tool),
                "file_info": {"mode": 0o755},
            }
            for tool in self.tools
        }
        # nfpm reads a symlink entry's `src` as the link's target.
        contents |= {
            dst: {"src": target, "dst": dst, "type": "symlink"}
            for dst, target in self.links.items()
        }
        spec = {
            "name": self.name,
            "arch": arch,
            "platform": "linux",
            "version": VERSION_FIELD,
            # nfpm's default, `semver`, would build `0.<count>+g<sha>` as `0.<count>.0+g<sha>`.
            "version_schema": "none",
            "maintainer": "homelab",
            "description": self.description or self.name,
            "depends": self.depends,
            "contents": [contents[dst] for dst in sorted(contents)],
        }
        if self.scripts:
            spec["scripts"] = {
                MAINTAINER_SCRIPTS[name]: f"scripts/{name}"
                for name in sorted(self.scripts)
            }
        return yaml.safe_dump(spec, sort_keys=False)
