"""Host and package modes: one host's package, or one shared package, rendered from the git tree.

`render_host` reads `hosts/<host>/package.yml` and `render_package` reads
`hosts/packages/<pkg>/package.yml`. Each returns the package's files in C1's layout:
`hosts/<host>/` or `packages/<pkg>/`, holding `root/`, the tree the package installs, and
`nfpm.yaml`, its spec. `write_packages` writes them under `--out`.

A package lists its components, and may carry files and defaults of its own, laid out as a
component's are. A component directory under `hosts/components/` holds a `component.yml`::

    defaults: {name: value}         # beneath group_vars/all.yml (H3, *Component variables*)
    files:
      - {src: templates/x.sh.j2, dst: /usr/lib/homelab/x, mode: "0755"}
      - {lib: homelab-updater, dst: /usr/lib/homelab/homelab-updater, mode: "0755"}

A `src` ending `.j2` renders through the renderer's strict environment. Any other `src` is
copied byte for byte. A `lib` entry copies `hosts/lib/<name>`, a script several packages ship,
byte for byte; it is the one source outside the component a file entry may name.

A package spec may also list `maintainer_scripts`, each rendered from
`hosts/lib/maintainer-scripts/<name>.j2` into the package's `scripts/`, outside `root/`, and
named in `nfpm.yaml`'s `scripts:` (H3, *Package rules*).

Host mode renders no secret, because no substitution step follows it (H3, *Host mode renders
no secret*). The context withholds every top-level key of `ansible/vars/secrets.yml`, and
resolves under the strict undefined class, so an inventory value that reads a secret is
dropped too. A template that reads either fails the render, and the error names the secret
key. A `${` in a template is shell, and renders byte for byte: nothing here imports
`placeholders.py`.

Typical usage example:

    packages = [render_host("daniel-pi"), render_package("homelab-updater")]
    write_packages(packages, Path("/tmp/out"))
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import cast

import jinja2
import yaml

from lib import yaml_fast
from lib.ansible_jinja_env import SHARED_TPL
from lib.estate import REPO_INVENTORY, Inventory
from lib.render_context import UnresolvedVarsError, render_context
from lib.render_guard import StrictChainableUndefined, load_yaml
from lib.repo_paths import ANSIBLE, REPO
from renderer.renderer_lib.cluster import RenderRefused
from renderer.renderer_lib.environment import RenderError, render_file, renderer_env

HOSTS = REPO / "hosts"
SECRETS = ANSIBLE / "vars" / "secrets.yml"

# Every host package runs reconcile under /usr/bin/python3 (H3, *Package rules*).
HOST_DEPENDS = ("python3 (>= 3.12)",)
PACKAGE_KEYS = frozenset(
    {"components", "defaults", "depends", "description", "files", "maintainer_scripts"}
)
COMPONENT_KEYS = frozenset({"defaults", "files"})
# The builder exports the version (H3, *Package rules*). Kept out of the render, so a render
# changes only when the package's content does, and nfpm expands it at build time.
VERSION_FIELD = "${HOMELAB_VERSION}"

# A host, package or component name becomes a directory under `hosts/`, so it is one plain
# path piece: no `/`, no `..`, no leading dot or dash.
_PLAIN_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")

_UNDEFINED = re.compile(r"'([^']+)' is undefined")

# Each maintainer script a spec may list, and the nfpm `scripts:` key that installs it. `preinst`
# refuses a hostname mismatch, so only a host package may list it.
MAINTAINER_SCRIPTS = {
    "preinst": "preinstall",
    "postinst": "postinstall",
    "prerm": "preremove",
}
UNIT_DIR = PurePosixPath("/usr/lib/systemd/system")
UNIT_SUFFIXES = (".service", ".timer", ".socket", ".path")


@dataclass
class Package:
    """One rendered package: its name, its C1 directory, and every file it installs.

    Attributes:
        name: The Debian package name.
        directory: `hosts/<host>` or `packages/<pkg>`, relative to `--out`.
        files: Each installed path mapped to its bytes and its octal mode.
        depends: The `Depends` entries, in the order the spec lists them.
        description: The package description.
        scripts: Each maintainer script's name, such as `postinst`, mapped to its bytes.
    """

    name: str
    directory: str
    files: dict[str, tuple[bytes, int]] = field(default_factory=dict)
    depends: list[str] = field(default_factory=list)
    description: str = ""
    scripts: dict[str, bytes] = field(default_factory=dict)

    def units(self) -> list[str]:
        """The unit files the package installs under `/usr/lib/systemd/system/`, by name.

        A drop-in and an enable link sit one directory deeper, so neither is listed.
        """
        return sorted(
            PurePosixPath(dst).name
            for dst in self.files
            if PurePosixPath(dst).parent == UNIT_DIR and dst.endswith(UNIT_SUFFIXES)
        )

    def nfpm_spec(self) -> str:
        """The `nfpm.yaml` the builder runs `nfpm package` on, from the package directory."""
        spec = {
            "name": self.name,
            "arch": "all",
            "platform": "linux",
            "version": VERSION_FIELD,
            "maintainer": "homelab",
            "description": self.description or self.name,
            "depends": self.depends,
            "contents": [
                {"src": f"root{dst}", "dst": dst, "file_info": {"mode": mode}}
                for dst, (_, mode) in sorted(self.files.items())
            ],
        }
        if self.scripts:
            spec["scripts"] = {
                MAINTAINER_SCRIPTS[name]: f"scripts/{name}"
                for name in sorted(self.scripts)
            }
        return yaml.safe_dump(spec, sort_keys=False)


def secret_names(secrets: Path = SECRETS) -> frozenset[str]:
    """Every top-level key name of the ciphertext file, `NEVER_STUB` names included."""
    return frozenset(str(key) for key in load_yaml(secrets) if key != "sops")


def render_host(
    host: str,
    hosts_root: Path = HOSTS,
    inventory: Inventory = REPO_INVENTORY,
    secrets: Path = SECRETS,
) -> Package:
    """`hosts/<host>/package.yml` rendered with `host`'s inventory layers.

    Raises:
        RenderRefused: The host has no package, or the spec is malformed.
        RenderError: A template failed to render.
    """
    spec = hosts_root / _plain_name(host, "host") / "package.yml"
    package = Package(name=f"homelab-{host}", directory=f"hosts/{host}")
    package.depends.extend(HOST_DEPENDS)
    return _render(package, spec, host, hosts_root, inventory, secret_names(secrets))


def render_package(
    name: str,
    hosts_root: Path = HOSTS,
    inventory: Inventory = REPO_INVENTORY,
    secrets: Path = SECRETS,
) -> Package:
    """`hosts/packages/<name>/package.yml`, rendered with `group_vars/all.yml` and no host.

    Raises:
        RenderRefused: The package does not exist, or the spec is malformed.
        RenderError: A template failed to render.
    """
    spec = hosts_root / "packages" / _plain_name(name, "package") / "package.yml"
    package = Package(name=name, directory=f"packages/{name}")
    return _render(package, spec, None, hosts_root, inventory, secret_names(secrets))


def write_packages(packages: list[Package], out: Path) -> list[Path]:
    """Write each package's `root/` and `nfpm.yaml` under `out`; return the files written.

    Raises:
        RenderRefused: A package directory already exists under `out`. Nothing is written.
    """
    for package in packages:
        if (out / package.directory).exists():
            raise RenderRefused(
                f"{out / package.directory} exists; render into an empty --out"
            )
    written = []
    for package in packages:
        base = out / package.directory
        for dst, (content, mode) in sorted(package.files.items()):
            path = base / "root" / dst.lstrip("/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(mode)
            written.append(path)
        for name, content in sorted(package.scripts.items()):
            path = base / "scripts" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(0o755)
            written.append(path)
        (base / "nfpm.yaml").write_text(package.nfpm_spec())
        written.append(base / "nfpm.yaml")
    return written


def _render(
    package: Package,
    spec_path: Path,
    host: str | None,
    hosts_root: Path,
    inventory: Inventory,
    withheld: frozenset[str],
) -> Package:
    if not spec_path.is_file():
        raise RenderRefused(
            f"{spec_path.relative_to(hosts_root.parent)} does not exist"
        )
    spec = _load(spec_path, PACKAGE_KEYS)
    package.description = str(spec.get("description", ""))
    package.depends.extend(str(entry) for entry in spec.get("depends") or [])
    lib = hosts_root / "lib"
    _render_unit(package, spec_path.parent, spec, host, inventory, withheld, lib)
    for name in spec.get("components") or []:
        directory = hosts_root / "components" / _plain_name(name, "component")
        component = directory / "component.yml"
        if not component.is_file():
            raise RenderRefused(f"{spec_path} lists component {name!r}, which has none")
        unit = _load(component, COMPONENT_KEYS)
        _render_unit(package, directory, unit, host, inventory, withheld, lib)
    _render_maintainer_scripts(
        package, spec.get("maintainer_scripts") or [], host, inventory, withheld, lib
    )
    return package


def _render_maintainer_scripts(
    package: Package,
    names: list,
    host: str | None,
    inventory: Inventory,
    withheld: frozenset[str],
    lib: Path,
) -> None:
    """Render each listed maintainer script from `hosts/lib/maintainer-scripts/`.

    Each template sees the host's context plus `package_units`, the units the package
    installs, which is the list `prerm` stops.
    """
    directory = lib / "maintainer-scripts"
    for name in names:
        if name not in MAINTAINER_SCRIPTS:
            raise RenderRefused(
                f"{package.name}: maintainer script {name!r} is not one of "
                f"{sorted(MAINTAINER_SCRIPTS)}"
            )
        if name == "preinst" and host is None:
            raise RenderRefused(
                f"{package.name}: preinst refuses a hostname mismatch, "
                "so only a host package lists it"
            )
        src = f"{name}.j2"
        _confined(directory, src)
        ctx, reasons = host_context(host, {}, inventory, withheld)
        ctx["package_units"] = package.units()
        env = renderer_env(directory)
        env.loader = _ConfinedLoader(cast(jinja2.BaseLoader, env.loader), directory)
        package.scripts[name] = _render_template(
            env, src, ctx, reasons, directory
        ).encode()


def _plain_name(name: object, kind: str) -> str:
    """`name` if it is one plain path piece, which keeps a joined directory under its root."""
    if not isinstance(name, str) or not _PLAIN_NAME.fullmatch(name):
        raise RenderRefused(
            f"{kind} name {name!r} is not a plain name (lowercase letters, digits, hyphens)"
        )
    return name


def _load(path: Path, allowed: frozenset[str]) -> dict:
    spec = yaml_fast.safe_load(path.read_text()) or {}
    if not isinstance(spec, dict):
        raise RenderRefused(f"{path} is not a mapping")
    unknown = sorted(set(spec) - allowed)
    if unknown:
        raise RenderRefused(f"{path} has unknown keys {unknown}")
    return spec


def _render_unit(
    package: Package,
    directory: Path,
    spec: dict,
    host: str | None,
    inventory: Inventory,
    withheld: frozenset[str],
    lib: Path,
) -> None:
    """Render the files `spec` lists from `directory`, or `lib`, into `package`."""
    files = spec.get("files") or []
    if not files:
        return
    ctx, reasons = host_context(host, spec.get("defaults") or {}, inventory, withheld)
    env = renderer_env(directory)
    env.loader = _ConfinedLoader(cast(jinja2.BaseLoader, env.loader), directory)
    for entry in files:
        sources = {"src", "lib"} & set(entry)
        if "dst" not in entry or not sources:
            raise RenderRefused(f"{directory}: file entry {entry} lacks src or dst")
        if len(sources) > 1:
            raise RenderRefused(
                f"{directory}: file entry {entry} names both src and lib"
            )
        dst = str(entry["dst"])
        destination = PurePosixPath(dst)
        if not destination.is_absolute() or ".." in destination.parts:
            raise RenderRefused(
                f"{directory}: dst {dst!r} is not a plain absolute path"
            )
        if dst in package.files:
            raise RenderRefused(f"{package.name}: two files install {dst}")
        mode = _mode(entry.get("mode", "0644"), directory, dst)
        if "lib" in entry:
            # A plain name has no `.j2` suffix, so a lib script is copied, never rendered.
            source = _confined(lib, _plain_name(entry["lib"], "lib script"))
            package.files[dst] = (source.read_bytes(), mode)
            continue
        src = str(entry["src"])
        source = _confined(directory, src)
        if src.endswith(".j2"):
            content = _render_template(env, src, ctx, reasons, directory).encode()
        else:
            content = source.read_bytes()
        package.files[dst] = (content, mode)


def _confined(directory: Path, src: str) -> Path:
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


class _ConfinedLoader(jinja2.BaseLoader):
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


def _mode(value: object, directory: Path, dst: str) -> int:
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


def host_context(
    host: str | None,
    defaults: dict,
    inventory: Inventory = REPO_INVENTORY,
    withheld: frozenset[str] = frozenset(),
) -> tuple[dict, dict[str, str]]:
    """The context a component renders with, and why each name it lacks is missing.

    The second value maps each withheld secret name, and each inventory value dropped
    because it would not expand, to the reason a template reading it gets.
    """

    def resolve(strict: bool) -> dict:
        return render_context(
            None,
            host,
            strict=strict,
            inventory=inventory,
            base={},
            undefined_cls=StrictChainableUndefined,
            component_defaults=defaults,
            withhold=withheld,
            # A play sets it before any variable expands, so a default may read it.
            overrides={"inventory_hostname": host} if host else None,
        )

    reasons = {
        name: "a secret key, and host mode renders no secret" for name in withheld
    }
    try:
        ctx = resolve(strict=True)
    except UnresolvedVarsError as exc:
        # Not raised: group_vars hold values only a play can expand (`ansible_check_mode`),
        # and a render fails only for a template that reads one.
        reasons |= {
            key: f"its inventory value does not expand in host mode ({reason})"
            for key, reason in exc.keys.items()
        }
        ctx = resolve(strict=False)
    ctx["inventory_hostname"] = host
    return ctx, reasons


def _render_template(
    env: jinja2.Environment,
    src: str,
    ctx: dict,
    reasons: dict[str, str],
    directory: Path,
) -> str:
    try:
        return render_file(env, src, ctx)
    except jinja2.UndefinedError as exc:
        match = _UNDEFINED.search(str(exc))
        name = match.group(1) if match else ""
        detail = f": {name} is {reasons[name]}" if name in reasons else ""
        raise RenderError(f"{directory / src}: {exc}{detail}") from exc
    except jinja2.TemplateError as exc:
        raise RenderError(f"{directory / src}: {exc}") from exc
