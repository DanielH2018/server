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

A `src` ending `.j2` renders through the renderer's strict environment. Any other `src` is
copied byte for byte.

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

import jinja2
import yaml

from lib import yaml_fast
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
PACKAGE_KEYS = frozenset({"components", "defaults", "depends", "description", "files"})
COMPONENT_KEYS = frozenset({"defaults", "files"})
# The builder exports the version (H3, *Package rules*). Kept out of the render, so a render
# changes only when the package's content does, and nfpm expands it at build time.
VERSION_FIELD = "${HOMELAB_VERSION}"

_UNDEFINED = re.compile(r"'([^']+)' is undefined")


@dataclass
class Package:
    """One rendered package: its name, its C1 directory, and every file it installs.

    Attributes:
        name: The Debian package name.
        directory: `hosts/<host>` or `packages/<pkg>`, relative to `--out`.
        files: Each installed path mapped to its bytes and its octal mode.
        depends: The `Depends` entries, in the order the spec lists them.
        description: The package description.
    """

    name: str
    directory: str
    files: dict[str, tuple[bytes, int]] = field(default_factory=dict)
    depends: list[str] = field(default_factory=list)
    description: str = ""

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
    spec = hosts_root / host / "package.yml"
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
    spec = hosts_root / "packages" / name / "package.yml"
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
    _render_unit(package, spec_path.parent, spec, host, inventory, withheld)
    for name in spec.get("components") or []:
        directory = hosts_root / "components" / name
        component = directory / "component.yml"
        if not component.is_file():
            raise RenderRefused(f"{spec_path} lists component {name!r}, which has none")
        unit = _load(component, COMPONENT_KEYS)
        _render_unit(package, directory, unit, host, inventory, withheld)
    return package


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
) -> None:
    """Render the files `spec` lists from `directory` into `package`."""
    files = spec.get("files") or []
    if not files:
        return
    ctx, reasons = host_context(host, spec.get("defaults") or {}, inventory, withheld)
    env = renderer_env(directory)
    for entry in files:
        src, dst = str(entry["src"]), str(entry["dst"])
        destination = PurePosixPath(dst)
        if not destination.is_absolute() or ".." in destination.parts:
            raise RenderRefused(
                f"{directory}: dst {dst!r} is not a plain absolute path"
            )
        if dst in package.files:
            raise RenderRefused(f"{package.name}: two files install {dst}")
        mode = int(str(entry.get("mode", "0644")), 8)
        if src.endswith(".j2"):
            content = _render_template(env, src, ctx, reasons, directory).encode()
        else:
            content = (directory / src).read_bytes()
        package.files[dst] = (content, mode)


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
