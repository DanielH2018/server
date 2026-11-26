"""Where a host package's files may come from, and the octal mode each installs with.

`host.py` renders a package spec's file entries. This module holds the rules that keep every
source inside its root: `confined` for a file entry's `src`, `ConfinedLoader` for a template
that loads another by name, and `SHARED_FILES`, the allowlist a `shared` entry names. A
component therefore cannot copy a file of the rendering machine, or of the repo outside the
allowlist, into a package.
"""

import re
from pathlib import Path
from typing import cast

import jinja2

from lib.guard_jinja_env import SHARED_TPL
from lib.estate import role_defaults
from renderer.renderer_lib.cluster import RenderRefused
from renderer.renderer_lib.environment import strict_env

# A file two components ship stays single here; a named allowlist so a component cannot copy any
# other repo file.
_LONGHORN_LIB = "scripts/lib/longhorn_lib"
SHARED_FILES = {
    "homelab-check-lib.sh": "hosts/components/check-lib/files/homelab-check-lib.sh",
    "etcd_restore_drill.sh": "scripts/backup/etcd_restore_drill.sh",
    # What the backup-health reader imports; other tools import the same modules from the checkout,
    # so they stay single in scripts/lib/.
    "k8s_conditions.py": "scripts/lib/k8s_conditions.py",
    "k8s_objects.py": "scripts/lib/k8s_objects.py",
    "longhorn_backup_health_logic.py": f"{_LONGHORN_LIB}/longhorn_backup_health_logic.py",
    "longhorn_backups.py": f"{_LONGHORN_LIB}/longhorn_backups.py",
    "longhorn_cluster_state.py": f"{_LONGHORN_LIB}/longhorn_cluster_state.py",
    "longhorn_cron_evidence_logic.py": f"{_LONGHORN_LIB}/longhorn_cron_evidence_logic.py",
    "longhorn_etcd_s3_logic.py": f"{_LONGHORN_LIB}/longhorn_etcd_s3_logic.py",
    "longhorn_restore_drill_stamps.py": f"{_LONGHORN_LIB}/longhorn_restore_drill_stamps.py",
    "longhorn_skipped_volumes_logic.py": f"{_LONGHORN_LIB}/longhorn_skipped_volumes_logic.py",
}

# Each file stays single in its owner so two renderers cannot fork it; a named allowlist so a spec
# cannot read any other component. claude-code's agent user and per-uid slice cap sit in each host's
# package.yml, because the uid differs per host.
COMPONENT_SOURCES = frozenset({"claude-code", "k3s-server", "pi-resolver"})
# A `src` with this prefix names a path inside the spec's `component:`.
COMPONENT_SRC = "component:"


def component_sources(spec: dict, directory: Path) -> tuple[dict[str, Path], dict]:
    """The directory each source prefix of `spec` loads from, and the defaults it renders with.

    The first value maps `component:` to the borrowed component's directory. The defaults are,
    lowest first: the spec's own `defaults/main.yml`, the `component:` directory's
    `defaults/main.yml`, then the spec's `defaults:`. Inventory still sits above all of them, so
    a `host_vars` value wins (H3's *Component variables*).

    Raises:
        RenderRefused: The component is not one of `COMPONENT_SOURCES`.
    """
    defaults = spec.get("defaults") or {}
    own = role_defaults(directory)
    component = spec.get("component")
    if component is None:
        return {}, {**own, **defaults}
    if component not in COMPONENT_SOURCES:
        raise RenderRefused(
            f"{directory}: component {component!r} is not one of {sorted(COMPONENT_SOURCES)}"
        )
    # A component sits in hosts/components/, and a package.yml in hosts/<host>/ beside it.
    parent = directory.parent
    source = (
        parent if parent.name == "components" else parent / "components"
    ) / component
    return {COMPONENT_SRC: source}, {**own, **role_defaults(source), **defaults}


def template_source(
    dirs: dict[str, Path], src: str, directory: Path
) -> tuple[Path, str]:
    """The directory and name `src` loads from: the component, or the component it borrows.

    Raises:
        RenderRefused: `src` names a borrowed component and the spec lists none.
    """
    if not src.startswith(COMPONENT_SRC):
        return directory, src
    if COMPONENT_SRC not in dirs:
        raise RenderRefused(
            f"{directory}: src {src!r} names a component, and it lists none"
        )
    return dirs[COMPONENT_SRC], src[len(COMPONENT_SRC) :]


def confined_env(directory: Path) -> jinja2.Environment:
    """The renderer's strict environment over `directory`, with its loader confined to it."""
    env = strict_env(directory)
    env.loader = ConfinedLoader(cast(jinja2.BaseLoader, env.loader), directory)
    return env


# A `lib` entry names a script, a package module such as `reconcile_lib/ufw.py`, or a top-level
# module; a module is lowercase with underscores and ends `.py`.
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
    directory and the shared `k8s/templates/` the environment also searches. Nothing else
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
