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
# SHARED_FILES, so a component cannot read any other role. `initial_setup` joined in H7.30, which
# moved the Pi's fail2ban group, maintenance jobs and ufw. H7.31 moved daniel-server's and H7.32
# daniel-box's; the role still applies its other subjects on daniel-box.
ROLE_SOURCES = frozenset({"common", "initial_setup", "nut_host"})
# The components a host's package.yml may borrow sources from with `component:`. claude-code's
# agent user and its per-uid slice cap sit in each host's package.yml, because the uid differs
# per host, and they render the component's defaults and templates (#6773). A named allowlist,
# like ROLE_SOURCES.
COMPONENT_SOURCES = frozenset({"claude-code"})
# A `src` with this prefix names a template of the component's `role:`, not of the component.
ROLE_SRC = "role:"
# A `src` with this prefix names a static file under the role's `files/`, copied as it stands.
ROLE_FILE_SRC = "role-file:"
# A `src` with this prefix names a path inside the spec's `component:`, such as
# `component:templates/x.j2`, which loads from that component as its own `src` would.
COMPONENT_SRC = "component:"


def role_sources(spec: dict, directory: Path) -> tuple[dict[str, Path], dict]:
    """The directory each source prefix of `spec` loads from, and the defaults it renders with.

    The first value maps `role:`/`role-file:` to the role's `templates/`/`files/`, or
    `component:` to the borrowed component's directory. The defaults are, lowest first: the
    spec's own `defaults/main.yml`, the `role:` or `component:` directory's `defaults/main.yml`,
    then the spec's `defaults:`. Inventory still sits above all of them, so a `host_vars` value
    wins (H3's *Component variables*).

    Raises:
        RenderRefused: The role is not one of `ROLE_SOURCES`, the component is not one of
            `COMPONENT_SOURCES`, or the spec names both.
    """
    defaults = spec.get("defaults") or {}
    own = role_defaults(directory)
    role = spec.get("role")
    component = spec.get("component")
    if role is not None and component is not None:
        raise RenderRefused(
            f"{directory}: names both role {role!r} and component {component!r}"
        )
    if role is not None:
        if role not in ROLE_SOURCES:
            raise RenderRefused(
                f"{directory}: role {role!r} is not one of {sorted(ROLE_SOURCES)}"
            )
        role_dir = ANSIBLE / "roles" / "setup" / str(role)
        dirs = {ROLE_SRC: role_dir / "templates", ROLE_FILE_SRC: role_dir / "files"}
        return dirs, {**own, **role_defaults(role_dir), **defaults}
    if component is not None:
        if component not in COMPONENT_SOURCES:
            raise RenderRefused(
                f"{directory}: component {component!r} is not one of {sorted(COMPONENT_SOURCES)}"
            )
        # A package.yml sits in hosts/<host>/, beside hosts/components/.
        source = directory.parent / "components" / str(component)
        return {COMPONENT_SRC: source}, {**own, **role_defaults(source), **defaults}
    return {}, {**own, **defaults}


def template_source(
    dirs: dict[str, Path], src: str, directory: Path
) -> tuple[Path, str]:
    """The directory and name `src` loads from: the component, or the role or component it borrows.

    Raises:
        RenderRefused: `src` names a role or a component the spec does not list, or a
            `role-file:` src names a template.
    """
    prefix = next(
        (p for p in (ROLE_SRC, ROLE_FILE_SRC, COMPONENT_SRC) if src.startswith(p)), None
    )
    if prefix is None:
        return directory, src
    if prefix not in dirs:
        kind = "component" if prefix == COMPONENT_SRC else "role"
        raise RenderRefused(
            f"{directory}: src {src!r} names a {kind}, and it lists none"
        )
    name = src[len(prefix) :]
    if prefix == ROLE_FILE_SRC and name.endswith(".j2"):
        raise RenderRefused(f"{directory}: src {src!r} is a template; name it role:")
    return dirs[prefix], name


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
