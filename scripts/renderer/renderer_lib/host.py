"""Host and package modes: one host's package, or one shared package, rendered from the git tree.

`render_host` reads `hosts/<host>/package.yml` and `render_package` reads
`hosts/packages/<pkg>/package.yml`. Each returns the package's files in C1's layout:
`hosts/<host>/` or `packages/<pkg>/`, holding `root/`, the tree the package installs, and
`nfpm.yaml`, its spec. `write_packages` writes them under `--out`.

A package lists its components, and may carry files and defaults of its own, laid out as a
component's are. A component directory under `hosts/components/` holds a `component.yml`::

    defaults: {name: value}         # beneath group_vars/all.yml (H3, *Component variables*)
    role: nut_host                  # its defaults sit below `defaults`; `role:<t>.j2` is its template
    files:
      - {src: templates/x.sh.j2, dst: /usr/lib/homelab/x, mode: "0755"}
      - {lib: homelab-updater, dst: /usr/lib/homelab/homelab-updater, mode: "0755"}
      - {key: homelab-archive-keyring, dst: /usr/share/keyrings/homelab-archive-keyring.gpg}

A `src` ending `.j2` renders through the renderer's strict environment, and a `src` of
`role:<name>` loads from the component's role templates. Any other `src` is copied byte for byte,
as are the three sources outside the component a file entry may name: a `lib` entry
(`hosts/lib/<name>`, a script several packages ship), a `key` entry (`hosts/keys/<name>.gpg`, a
public keyring hosts trust) and a `shared` entry (one of the `SHARED_FILES`, a file an Ansible role
still installs).

A package or component may also carry `units`, `enable` and `legacy` (host_manifest.py), a
file entry's `activate:` field and a `ufw:` map (host_arms.py). A host package installs
`/usr/lib/homelab/manifest.json`, which `host_manifest` builds from them and from the units and
enable links the packages install. A shared package installs no manifest, and lists no legacy
entry, no enable link and no ufw rule.

A component may list `depends:`, `Depends` entries its package gains.
A package spec may also list `maintainer_scripts`, each rendered from
`hosts/lib/maintainer-scripts/<name>.j2` into the package's `scripts/`, outside `root/`, and
named in `nfpm.yaml`'s `scripts:` (H3, *Package rules*).

Host mode renders no secret, except in two kinds of file (H4, *Secret-bearing files*): a
spec's declared `outputs:` and its `compose: true` files render a secret reference as `${<key>}`
with C1's escape, through `host_outputs.py`. Everywhere else no substitution step follows, so
the context withholds every top-level key of `ansible/vars/secrets.yml`, and resolves under the
strict undefined class, so an inventory value that reads a secret is dropped too. A template
that reads either fails the render, and the error names the secret key. A `${` in such a
template is shell, and renders byte for byte.

Typical usage example:

    packages = [render_host("daniel-pi"), render_package("homelab-updater")]
    write_packages(packages, Path("/tmp/out"))
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import re
from pathlib import Path, PurePosixPath

import jinja2

from lib import yaml_fast
from lib.estate import REPO_INVENTORY, Inventory
from lib.render_context import UnresolvedVarsError, render_context
from lib.render_guard import StrictChainableUndefined, load_yaml
from lib.repo_paths import ANSIBLE, REPO
from renderer.renderer_lib.cluster import RenderRefused
from renderer.renderer_lib.environment import RenderError, render_file
from renderer.renderer_lib.host_sources import (
    SHARED_FILES,
    confined,
    confined_env,
    file_mode,
    lib_source,
    role_sources,
    template_source,
)
from renderer.renderer_lib import host_compose
from renderer.renderer_lib.host_arms import ARM_KEYS, add_activation, add_arms, has_arms
from renderer.renderer_lib.host_outputs import (
    SECRETS_COMPONENT,
    add_credentials,
    add_output,
    compose_file,
    finish,
)
from renderer.renderer_lib.host_package import (
    MAINTAINER_SCRIPTS,
    VERSION_FIELD as VERSION_FIELD,
    UNIT_SUFFIXES as UNIT_SUFFIXES,
    Package,
)
from renderer.renderer_lib.placeholders import placeholder_vars
from renderer.renderer_lib.host_tools import parse_tools, tool_path
from renderer.renderer_lib.host_write import write_packages as write_packages
from renderer.renderer_lib.host_manifest import (
    DROPIN as DROPIN,
    UNIT_DIR as UNIT_DIR,
    UPDATER,
    add_enable_links,
    add_manifest,
    add_policies,
    legacy_entry,
    refuse_wants_file,
)

HOSTS = REPO / "hosts"
SECRETS = ANSIBLE / "vars" / "secrets.yml"
# The per-host ciphertext a package that ships `secrets-unit` carries (H4, *Which ciphertext*).
HOST_SOPS = ANSIBLE / "vars" / "hosts"

# Every host package runs reconcile under /usr/bin/python3 (H3, *Package rules*).
HOST_DEPENDS = ("python3 (>= 3.12)",)
PACKAGE_KEYS = frozenset(
    {
        "components",
        "credentials",
        "defaults",
        "depends",
        "description",
        "enable",
        "files",
        "legacy",
        "maintainer_scripts",
        "outputs",
        "tools",
        "units",
        *ARM_KEYS,
    }
)
COMPONENT_KEYS = frozenset(
    {
        "compose",
        "credentials",
        "defaults",
        "depends",
        "enable",
        "files",
        "legacy",
        "outputs",
        "role",
        "units",
        *ARM_KEYS,
    }
)

# A host, package or component name becomes a directory under `hosts/`, so it is one plain
# path piece: no `/`, no `..`, no leading dot or dash.
_PLAIN_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")

_UNDEFINED = re.compile(r"'([^']+)' is undefined")


def secret_names(secrets: Path = SECRETS) -> frozenset[str]:
    """Every top-level key name of the ciphertext file, `NEVER_STUB` names included."""
    return frozenset(str(key) for key in load_yaml(secrets) if key != "sops")


def render_host(
    host: str,
    hosts_root: Path = HOSTS,
    inventory: Inventory = REPO_INVENTORY,
    secrets: Path = SECRETS,
    sops_dir: Path = HOST_SOPS,
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
    finish(package, host, sops_dir)
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
    finish(package, None, HOST_SOPS)
    for dst in package.files:
        if any(dst == tool_path(tool) for tool in package.tools):
            raise RenderRefused(f"{name}: two files install {dst}")
    # Reconcile reads only the host package's manifest, so a shared one's would be dropped.
    if package.legacy:
        raise RenderRefused(
            f"{name}: a shared package lists no legacy entry; its host package does"
        )
    if package.links:
        raise RenderRefused(
            f"{name}: a shared package lists no enable link; its host package does"
        )
    if has_arms(package):
        raise RenderRefused(
            f"{name}: a shared package declares no ufw, libvirt, users or lines"
        )
    return package


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
    if "tools" in spec:
        if host is not None:
            raise RenderRefused(
                f"{spec_path}: a host package lists no tools; a shared package does"
            )
        package.tools = parse_tools(spec["tools"], str(spec_path))
    package.secrets = SECRETS_COMPONENT in (spec.get("components") or [])
    lib = hosts_root / "lib"
    _render_unit(package, spec_path.parent, spec, host, inventory, withheld, lib)
    for name in spec.get("components") or []:
        directory = hosts_root / "components" / _plain_name(name, "component")
        component = directory / "component.yml"
        if not component.is_file():
            raise RenderRefused(f"{spec_path} lists component {name!r}, which has none")
        unit = _load(component, COMPONENT_KEYS)
        package.depends.extend(
            str(dep) for dep in unit.get("depends") or [] if dep not in package.depends
        )
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

    Each template sees the host's context plus `package_units`, the list of units `prerm` stops.
    A template unit such as `homelab-alert@.service` is no unit to stop, so the list omits it.
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
        ctx["package_units"] = [
            unit for unit in package.units() if not unit.endswith("@.service")
        ]
        env = confined_env(directory)
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
    add_credentials(package, directory, spec.get("credentials") or [], withheld)
    enable = spec.get("enable") or {}
    legacy = spec.get("legacy") or []
    outputs = spec.get("outputs") or []
    arms = {key: spec[key] for key in ARM_KEYS if spec.get(key)}
    role, defaults = role_sources(spec, directory)
    compose = spec.get("compose")
    if not (files or legacy or enable or outputs or arms or compose):
        return
    ctx, reasons = host_context(host, defaults, inventory, withheld)
    env = confined_env(directory)
    add_enable_links(
        package,
        directory,
        enable,
        lambda expression: _evaluate(env, expression, ctx, reasons, directory),
    )
    add_arms(
        package,
        directory,
        arms,
        lambda text: _render_string(env, text, ctx, reasons, directory),
        lambda src: _render_template(env, src, ctx, reasons, directory),
    )
    for entry in legacy:
        package.legacy.append(
            legacy_entry(
                entry,
                directory,
                lambda text: _render_string(env, text, ctx, reasons, directory),
            )
        )

    def secret_render(base: Path, name: str, keys: frozenset[str], extra: dict) -> str:
        """`base/name` with the secrets in `keys` read as sentinels and `extra` in the context."""
        source = confined(base, name)
        if not name.endswith(".j2"):
            return source.read_text()
        sub, why = host_context(host, defaults, inventory, withheld, keys)
        sub |= {"secret_placeholders": {k: sub[k] for k in sorted(keys)}, **extra}
        return _render_template(confined_env(base), name, sub, why, base)

    def secret_text(src: str, keys: frozenset[str]) -> str:
        """`src` with the secrets in `keys` read as sentinels, for `host_outputs.py`."""
        return secret_render(*template_source(role, src, directory), keys, {})

    referenced = None
    if compose:
        data_root = f"/home/{ctx['sys_user']}/server/containers"
        referenced = host_compose.add_compose(
            package,
            directory,
            compose,
            ctx.get(host_compose.LIST_KEY),
            withheld,
            data_root,
            secret_render,
        )

    if not isinstance(outputs, list):
        raise RenderRefused(f"{directory}: outputs {outputs!r} is not a list")
    for entry in outputs:
        add_output(package, directory, entry, withheld, secret_text)
    if referenced is not None:
        host_compose.check_env_output(package, directory, referenced)
    keys = lib.parent / "keys"
    for entry in host_compose.deferred_last(files):
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
        add_activation(package, directory, dst, entry.get("activate"))
        if dst in package.files:
            raise RenderRefused(f"{package.name}: two files install {dst}")
        mode = file_mode(entry.get("mode", "0644"), directory, dst)
        ctx = host_compose.inputs_context(package, directory, entry, ctx)
        if entry.get("compose") is True:
            if "src" not in entry:
                raise RenderRefused(f"{directory}: compose file {dst} needs a src")
            package.files[dst] = (
                compose_file(directory, str(entry["src"]), dst, withheld, secret_text),
                mode,
            )
            continue
        if "lib" in entry:
            # A plain name has no `.j2` suffix, so a lib script is copied, never rendered.
            source = lib_source(lib, entry["lib"])
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
        base, src = template_source(role, str(entry["src"]), directory)
        source = confined(base, src)
        if src.endswith(".j2"):
            tenv = confined_env(base)
            content = _render_template(tenv, src, ctx, reasons, base).encode()
        else:
            content = source.read_bytes()
        package.files[dst] = (content, mode)


def host_context(
    host: str | None,
    defaults: dict,
    inventory: Inventory = REPO_INVENTORY,
    withheld: frozenset[str] = frozenset(),
    placeholders: frozenset[str] = frozenset(),
) -> tuple[dict, dict[str, str]]:
    """The context a component renders with, and why each name it lacks is missing.

    The second value maps each withheld secret name, and each inventory value dropped
    because it would not expand, to the reason a template reading it gets. A name in
    `placeholders` is not withheld: it reads as its sentinel (`placeholders.sentinel`), which
    a declared output or a compose file turns into `${<key>}`. The sentinel is an override, so
    an inventory value that aliases the secret expands to it too.
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
            withhold=withheld - placeholders,
            # A play sets it before any variable expands, so a default may read it.
            overrides={
                **placeholder_vars(placeholders),
                **({"inventory_hostname": host} if host else {}),
            }
            or None,
        )

    reasons = {
        name: "a secret key, and host mode renders no secret"
        for name in withheld - placeholders
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
        value = env.compile_expression(expression, undefined_to_none=False)(ctx)
        if isinstance(value, jinja2.Undefined):
            # An undefined name reaches here unraised; this raises the error naming it.
            value._fail_with_undefined_error()
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
