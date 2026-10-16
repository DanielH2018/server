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
      - {key: homelab-archive-keyring, dst: /usr/share/keyrings/homelab-archive-keyring.gpg}

A `src` ending `.j2` renders through the renderer's strict environment. Any other `src` is
copied byte for byte. A `lib` entry copies `hosts/lib/<name>`, a script several packages ship,
byte for byte. A `key` entry copies `hosts/keys/<name>.gpg`, a public keyring hosts trust, byte
for byte. A `shared` entry copies one of the `SHARED_FILES`, a file an Ansible role still
installs, byte for byte. Those three are the only sources outside the component a file entry
may name.

A package or component may also carry `units`, a policy for each unit reconcile must not
restart on a change, `enable`, the enable links it installs, and `legacy`, the Ansible-era
entries reconcile retires (H3, *The host handoff rule*)::

    units: {homelab-x.service: {policy: manual}}    # or windowed
    enable:                                         # see host_manifest.add_enable_links
      homelab-x.timer: timers.target
      homelab-y.timer: {target: timers.target, when: x_enabled}
    legacy:
      - {path: /usr/local/bin/x.sh}
      - {cron: "X heartbeat", user: "{{ sys_user }}"}
      - {cron_file: x}
      - {unit: x.service}

Each legacy value renders with the host's context. An enable entry's `when:` is a Jinja
expression evaluated in that context, and the link ships only when it is true. Each enable link
ships as an nfpm `type: symlink` entry. A host package installs
`/usr/lib/homelab/manifest.json`, which `host_manifest` builds from these keys and the units and
enable links the packages install. A shared package installs no manifest, and lists no legacy
entry and no enable link.

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
from lib.estate import REPO_INVENTORY, Inventory
from lib.render_context import UnresolvedVarsError, render_context
from lib.render_guard import StrictChainableUndefined, load_yaml
from lib.repo_paths import ANSIBLE, REPO
from renderer.renderer_lib.cluster import RenderRefused
from renderer.renderer_lib.environment import RenderError, render_file, renderer_env
from renderer.renderer_lib.host_sources import (
    SHARED_FILES,
    ConfinedLoader,
    confined,
    file_mode,
)
from renderer.renderer_lib.host_manifest import (
    DROPIN,
    UNIT_DIR,
    UPDATER,
    add_enable_links,
    add_manifest,
    add_policies,
    legacy_entry,
    refuse_wants_file,
)

HOSTS = REPO / "hosts"
SECRETS = ANSIBLE / "vars" / "secrets.yml"

# Every host package runs reconcile under /usr/bin/python3 (H3, *Package rules*).
HOST_DEPENDS = ("python3 (>= 3.12)",)
PACKAGE_KEYS = frozenset(
    {
        "components",
        "defaults",
        "depends",
        "description",
        "enable",
        "files",
        "legacy",
        "maintainer_scripts",
        "units",
    }
)
COMPONENT_KEYS = frozenset({"defaults", "enable", "files", "legacy", "units"})
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
        """The units whose enable link, under `<target>.wants/`, the package installs."""
        return sorted({PurePosixPath(dst).name for dst in self.links})

    def nfpm_spec(self) -> str:
        """The `nfpm.yaml` the builder runs `nfpm package` on, from the package directory."""
        contents = {
            dst: {"src": f"root{dst}", "dst": dst, "file_info": {"mode": mode}}
            for dst, (_, mode) in self.files.items()
        }
        # nfpm reads a symlink entry's `src` as the link's target.
        contents |= {
            dst: {"src": target, "dst": dst, "type": "symlink"}
            for dst, target in self.links.items()
        }
        spec = {
            "name": self.name,
            "arch": "all",
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
    withheld = secret_names(secrets)
    _render(package, spec, host, hosts_root, inventory, withheld)
    updater = None
    if (hosts_root / "packages" / UPDATER / "package.yml").is_file():
        updater = render_package(UPDATER, hosts_root, inventory, secrets)
    add_manifest(package, updater)
    return package


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
    _render(package, spec, None, hosts_root, inventory, secret_names(secrets))
    # Reconcile reads only the host package's manifest, so a shared one's would be dropped.
    if package.legacy:
        raise RenderRefused(
            f"{name}: a shared package lists no legacy entry; its host package does"
        )
    if package.links:
        raise RenderRefused(
            f"{name}: a shared package lists no enable link; its host package does"
        )
    return package


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
        for dst, target in sorted(package.links.items()):
            path = base / "root" / dst.lstrip("/")
            path.parent.mkdir(parents=True, exist_ok=True)
            # A host package's link may point at the updater's unit, absent from this root.
            path.symlink_to(target)
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
        confined(directory, src)
        ctx, reasons = host_context(host, {}, inventory, withheld)
        ctx["package_units"] = package.units()
        env = renderer_env(directory)
        env.loader = ConfinedLoader(cast(jinja2.BaseLoader, env.loader), directory)
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
    """Render the files, policies, enable links and legacy entries `spec` lists into `package`.

    Files come from `directory`, from `lib`, from the `keys` directory beside `lib`, or from
    the `SHARED_FILES` under the repo root that holds `lib`'s parent.
    """
    files = spec.get("files") or []
    add_policies(package, directory, spec.get("units") or {})
    enable = spec.get("enable") or {}
    legacy = spec.get("legacy") or []
    if not files and not legacy and not enable:
        return
    ctx, reasons = host_context(host, spec.get("defaults") or {}, inventory, withheld)
    env = renderer_env(directory)
    env.loader = ConfinedLoader(cast(jinja2.BaseLoader, env.loader), directory)
    add_enable_links(
        package,
        directory,
        enable,
        lambda expression: _evaluate(env, expression, ctx, reasons, directory),
    )
    for entry in legacy:
        package.legacy.append(
            legacy_entry(
                entry,
                directory,
                lambda text: _render_string(env, text, ctx, reasons, directory),
            )
        )
    keys = lib.parent / "keys"
    for entry in files:
        sources = {"src", "lib", "key", "shared"} & set(entry)
        if "dst" not in entry or not sources:
            raise RenderRefused(f"{directory}: file entry {entry} lacks src or dst")
        if len(sources) > 1:
            raise RenderRefused(
                f"{directory}: file entry {entry} names more than one of "
                "src, lib, key and shared"
            )
        dst = str(entry["dst"])
        destination = PurePosixPath(dst)
        if not destination.is_absolute() or ".." in destination.parts:
            raise RenderRefused(
                f"{directory}: dst {dst!r} is not a plain absolute path"
            )
        refuse_wants_file(directory, destination)
        if dst in package.files:
            raise RenderRefused(f"{package.name}: two files install {dst}")
        mode = file_mode(entry.get("mode", "0644"), directory, dst)
        if "lib" in entry:
            # A plain name has no `.j2` suffix, so a lib script is copied, never rendered.
            source = confined(lib, _plain_name(entry["lib"], "lib script"))
            package.files[dst] = (source.read_bytes(), mode)
            continue
        if "key" in entry:
            # The name carries no suffix, so the plain-name rule holds and `.gpg` is fixed here.
            source = confined(keys, f"{_plain_name(entry['key'], 'key')}.gpg")
            package.files[dst] = (source.read_bytes(), mode)
            continue
        if "shared" in entry:
            name = entry["shared"]
            if name not in SHARED_FILES:
                raise RenderRefused(
                    f"{directory}: shared file {name!r} is not one of {sorted(SHARED_FILES)}"
                )
            source = lib.parent.parent / SHARED_FILES[name]
            package.files[dst] = (source.read_bytes(), mode)
            continue
        src = str(entry["src"])
        source = confined(directory, src)
        if src.endswith(".j2"):
            content = _render_template(env, src, ctx, reasons, directory).encode()
        else:
            content = source.read_bytes()
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
    except jinja2.TemplateError as exc:
        raise _explained(exc, f"{directory / src}", reasons) from exc


def _evaluate(
    env: jinja2.Environment,
    expression: str,
    ctx: dict,
    reasons: dict[str, str],
    directory: Path,
) -> bool:
    """A `when:` expression's value in `ctx`, which must be a boolean.

    A string is refused rather than tested for truth, because `"false"` is a true string.
    """
    try:
        value = env.compile_expression(expression)(ctx)
    except jinja2.TemplateError as exc:
        raise _explained(exc, f"{directory}: when {expression!r}", reasons) from exc
    if not isinstance(value, bool):
        raise RenderRefused(
            f"{directory}: when {expression!r} is {value!r}, not a boolean"
        )
    return value


def _render_string(
    env: jinja2.Environment,
    text: str,
    ctx: dict,
    reasons: dict[str, str],
    directory: Path,
) -> str:
    try:
        return env.from_string(text).render(ctx)
    except jinja2.TemplateError as exc:
        raise _explained(exc, f"{directory}: {text!r}", reasons) from exc


def _explained(
    exc: jinja2.TemplateError, where: str, reasons: dict[str, str]
) -> RenderError:
    """The render error for `exc`, naming why an undefined name is missing when known."""
    detail = ""
    if isinstance(exc, jinja2.UndefinedError):
        match = _UNDEFINED.search(str(exc))
        name = match.group(1) if match else ""
        detail = f": {name} is {reasons[name]}" if name in reasons else ""
    return RenderError(f"{where}: {exc}{detail}")
