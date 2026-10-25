"""Where a host package's files may come from, and the octal mode each installs with.

`host.py` renders a package spec's file entries. This module holds the rules that keep every
source inside its root: `confined` for a file entry's `src`, `ConfinedLoader` for a template
that loads another by name, and `SHARED_FILES`, the allowlist a `shared` entry names. A
component therefore cannot copy a file of the rendering machine, or of the repo outside the
allowlist, into a package.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import re
from pathlib import Path
from typing import cast

import jinja2

from lib.ansible_jinja_env import SHARED_TPL
from lib.estate import role_defaults
from lib.repo_paths import ANSIBLE
from renderer.renderer_lib.cluster import RenderRefused
from renderer.renderer_lib.environment import renderer_env

# Each file a `shared` entry may name, by its repo path. The file stays single at its Ansible
# path, because a role still installs it there: initial_setup installs kuma-push-lib.sh and
# homelab-check-lib.sh on every host, and the Pi heartbeats' package copies read the same bytes
# (H3.8b, and M6 B5 for the check library). A named
# allowlist rather than a path, so a component cannot copy any other file of the repo.
SHARED_FILES = {
    "kuma-push-lib.sh": "ansible/roles/setup/initial_setup/files/kuma-push-lib.sh",
    "homelab-check-lib.sh": "ansible/roles/setup/initial_setup/files/homelab-check-lib.sh",
}

# The setup roles whose defaults and templates a component may render with `role:` (H4, *NUT on
# daniel-server*). The role keeps owning the file until H7.8 moves daniel-box, so a package
# renders it in place and a copy under `hosts/` would fork it. A named allowlist, like
# SHARED_FILES, so a component cannot read any other role.
ROLE_SOURCES = frozenset({"common", "nut_host"})
# A `src` with this prefix names a template of the component's `role:`, not of the component.
ROLE_SRC = "role:"


def role_sources(spec: dict, directory: Path) -> tuple[Path | None, dict]:
    """The role directory a component's `role:` names, and its `defaults:` over that role's.

    The component's own `defaults:` sit above the role's and below inventory, so a `host_vars`
    value still wins (H3's *Component variables*).

    Raises:
        RenderRefused: The role is not one of `ROLE_SOURCES`.
    """
    defaults = spec.get("defaults") or {}
    role = spec.get("role")
    if role is None:
        return None, defaults
    if role not in ROLE_SOURCES:
        raise RenderRefused(
            f"{directory}: role {role!r} is not one of {sorted(ROLE_SOURCES)}"
        )
    role_dir = ANSIBLE / "roles" / "setup" / str(role)
    return role_dir, {**role_defaults(role_dir), **defaults}


def template_source(
    role_dir: Path | None, src: str, directory: Path
) -> tuple[Path, str]:
    """The directory and template name `src` loads from: the component, or its role's templates.

    Raises:
        RenderRefused: `src` names a role template and the component lists no `role:`.
    """
    if not src.startswith(ROLE_SRC):
        return directory, src
    if role_dir is None:
        raise RenderRefused(f"{directory}: src {src!r} names a role, and it lists none")
    return role_dir / "templates", src[len(ROLE_SRC) :]


def confined_env(directory: Path) -> jinja2.Environment:
    """The renderer's strict environment over `directory`, with its loader confined to it."""
    env = renderer_env(directory)
    env.loader = ConfinedLoader(cast(jinja2.BaseLoader, env.loader), directory)
    return env


# A `lib` entry names a script in hosts/lib/, or a module of a package there such as
# `reconcile_lib/ufw.py`, or a top-level module such as `k3s_gates.py` (H6.3b), which a script
# of the same directory imports. A module is lowercase with underscores and ends `.py`.
_LIB_NAME = re.compile(
    r"[a-z0-9][a-z0-9-]*|[a-z][a-z0-9_]*\.py|[a-z][a-z0-9_]*/[a-z_][a-z0-9_]*\.py"
)


def lib_source(lib: Path, name: object) -> Path:
    """The file a `lib` entry names, confined to `lib`.

    Raises:
        RenderRefused: `name` is not a script name or a `<package>/<module>.py` path.
    """
    if not isinstance(name, str) or not _LIB_NAME.fullmatch(name):
        raise RenderRefused(
            f"lib script name {name!r} is not a plain name, a <module>.py or a <package>/<module>.py path"
        )
    return confined(lib, name)


def confined(directory: Path, src: str) -> Path:
    """`src` resolved through any symlink, which must be a file inside `directory`.

    Both branches of a file entry call this before they read: a static `src` is read from the
    result, and a `.j2` `src` is checked here and then loaded by name. Neither an absolute
    path, a `..` nor a link out of the component can put a file of the rendering machine into
    a package. The refusal names the component and the `src`, never the target.
    """
    path = (directory / src).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise RenderRefused(f"{directory}: src {src!r} is outside the component")
    if not path.is_file():
        raise RenderRefused(f"{directory}: src {src!r} does not exist")
    return path


class ConfinedLoader(jinja2.BaseLoader):
    """The renderer's loader, refusing a template that resolves outside its search roots.

    `{% include %}`, `{% import %}` and `{% extends %}` load by name through the loader, and
    jinja's `FileSystemLoader` follows a symlink there. A link in the component to a file
    outside it would otherwise be read into the package. The roots are the component
    directory and the shared `ansible/templates/` the environment also searches. Nothing else
    in host mode reads a file a component names: `lookup` is registered only when the
    environment is given a context, and `_render_unit` does not give it one.
    """

    def __init__(self, inner: jinja2.BaseLoader, directory: Path) -> None:
        self.inner = inner
        self.directory = directory
        self.roots = [directory.resolve(), SHARED_TPL.resolve()]

    def get_source(self, environment, template):
        source, filename, uptodate = self.inner.get_source(environment, template)
        if filename is not None and not any(
            Path(filename).resolve().is_relative_to(root) for root in self.roots
        ):
            raise RenderRefused(
                f"{self.directory}: template {template!r} is outside the component"
            )
        return source, filename, uptodate


def file_mode(value: object, directory: Path, dst: str) -> int:
    """A file entry's octal `mode` string as an int.

    Only a quoted string is taken. YAML 1.1 loads an unquoted `0644` as the integer 420, which
    cannot be told apart from a literal 420, so an int is refused rather than guessed at.
    """
    if not isinstance(value, str) or not re.fullmatch(r"0?[0-7]{3,4}", value):
        raise RenderRefused(
            f"{directory}: {dst} mode {value!r} is not a quoted octal string such as '0644'"
        )
    mode = int(value, 8)
    special = mode & ~0o777
    if special:
        names = {0o4000: "setuid", 0o2000: "setgid", 0o1000: "sticky"}
        bits = ", ".join(
            f"{bit:#o} {name}" for bit, name in names.items() if special & bit
        )
        raise RenderRefused(
            f"{directory}: {dst} mode {value!r} sets bits above 0o777 ({bits}); "
            "no package installs a special bit"
        )
    return mode
