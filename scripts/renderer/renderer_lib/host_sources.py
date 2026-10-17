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

import jinja2

from lib.ansible_jinja_env import SHARED_TPL
from renderer.renderer_lib.cluster import RenderRefused

# Each file a `shared` entry may name, by its repo path. The file stays single at its Ansible
# path, because a role still installs it there: initial_setup installs kuma-push-lib.sh on
# every host, and the Pi heartbeats' package copy reads the same bytes (H3.8b). A named
# allowlist rather than a path, so a component cannot copy any other file of the repo.
SHARED_FILES = {
    "kuma-push-lib.sh": "ansible/roles/setup/initial_setup/files/kuma-push-lib.sh",
}

# Every repo path a host render reads, as a prefix relative to the repo root: a member ending
# `/` is a directory, any other is one file (H3, *Steps*, H3.9a item 1). A change under none of
# them leaves every host package's render as it was, which is what lets the landing tell a PR
# that may reach a host from one that cannot without rendering. `secrets.yml` is a member
# because host mode removes its key names (`host.secret_names`), and `kuma-push-lib.sh`
# because it is one of `SHARED_FILES`. `pyproject.toml` and `uv.lock` decide the environment
# `uv run` renders in. The last three are past the plan's list: ansible-core reads
# `ansible.cfg` from the render's working directory, and `homelab_render.py` imports the other
# two at load, for cluster mode. `test_renderer_host_inputs.py` records every tracked file a
# real host render opens and fails on one under no member.
HOST_INPUTS = (
    "hosts/",
    "ansible/inventory/",
    "ansible/filter_plugins/",
    "scripts/lib/",
    "scripts/renderer/",
    "pyproject.toml",
    "uv.lock",
    "ansible/vars/secrets.yml",
    SHARED_FILES["kuma-push-lib.sh"],
    "ansible.cfg",
    "scripts/deploy_tools/render_targets.py",
    "scripts/secrets_mgmt/cluster_vars_check.py",
)


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
