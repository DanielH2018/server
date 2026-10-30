"""The Pi's Compose project in host mode: the include renderer and the inputs hash (H5.2a1).

A component with `compose: <directory>` holds one tree per service under that directory,
`<directory>/<service>/`, and the host's inventory lists the services in `compose_services`. The
renderer writes, under `/usr/lib/homelab/compose/`:

- `<service>/compose.yaml`, rendered from `compose.yaml.j2` with `container_item` set to the
  service's `compose_services` entry. Every secret reads as a placeholder, and C1's escape
  applies (`host_outputs.compose_file`), so Compose does the one substitution this file gets.
- `<service>/<file>`, one per other file in the service tree. A `.j2` file renders with every
  secret withheld, at mode 0644, so a secret in such a file fails the render. Any other file is
  copied. The compose file's context holds `<service>_<stem>_sha256` for each of these files,
  with dashes in the service name written as underscores, so a label can carry the file's hash
  and `up -d` recreates the container when the file changes (alloy's `config.alloy` reads as
  `alloy_config_sha256`). The renderer therefore renders these files first.
- `compose.yaml`, the project: its `include:` names each service's file, the data directory the
  Ansible-era project used as `project_directory`, and `compose.env` as `env_file` when any
  compose file reads a secret.

The renderer refuses a compose file that Compose would substitute a second time. C1's escape
turns a `$` before `$` or `{` in a template into `$$`, which Compose reads back as written. A `$`
that opens neither `$$` nor `${<key>}` of a secret key, such as `$HOME`, stays as written, and
Compose would interpolate it. The renderer refuses it. The keys the compose files read must equal
the keys of the component's `format: dotenv` output, so the Compose env file carries exactly what
the project needs. `host_secrets.py` reads that list statically from `component.yml`.

A file entry marked `inputs_sha256: true` renders after every other file, with
`compose_inputs_sha256` in its context: the sha256 over each regular file the package installs
under `/usr/lib/homelab/compose/`, plus `INPUT_EXTRAS` when the package ships them. Any change to
the project therefore changes that file's bytes, and reconcile's restart set sees it (H4, *The
restart contract*). Such a file installs outside the compose directory, so it is not among
its own inputs. Its context also holds `compose_external_networks`, the sorted names of every
network a service's compose file declares `external: true`, so the unit can create each one the
project needs and no other (H7.26).
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import hashlib
import re
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath

from lib import yaml_fast
from renderer.renderer_lib.cluster import RenderRefused
from renderer.renderer_lib.host_outputs import COMPOSE_ROOT
from renderer.renderer_lib.host_package import Package
from renderer.renderer_lib.placeholders import Sentinels

# The inventory key that lists the project's services (H5, *The service list*).
LIST_KEY = "compose_services"
INPUTS_VAR = "compose_inputs_sha256"
NETWORKS_VAR = "compose_external_networks"
# Files outside the compose directory that the project's run reads. A package that ships none
# of them yet hashes without them, and each joins the hash when a later PR ships it.
INPUT_EXTRAS = (
    "/usr/lib/homelab/compose-run",
    "/usr/lib/homelab/compose-adopt",
    "/usr/lib/tmpfiles.d/homelab-compose.conf",
)
COMPOSE_FILE = "compose.yaml"
ENV_PATH = COMPOSE_ROOT / "compose.env"
_SERVICE = re.compile(r"[a-z0-9][a-z0-9-]*")
_DIRECTORY = re.compile(r"[a-z0-9][a-z0-9_-]*")
# A `$` run Compose reads: `$$`, a braced name, or anything else it would interpolate.
_DOLLAR = re.compile(r"\$\$|\$\{([A-Za-z0-9_]+)\}|\$")

# Renders `base/name` with the secrets in `keys` as sentinels and `extra` over the context.
Render = Callable[[Path, str, frozenset[str], dict], str]


def services(directory: Path, entries: object) -> list[dict]:
    """The `compose_services` entries, each a map with a plain unique `name`, sorted by name.

    Raises:
        RenderRefused: The key is missing, is not a list of such maps, or repeats a name.
    """
    if entries is None:
        raise RenderRefused(
            f"{directory}: compose needs `{LIST_KEY}` in the host's inventory, and the host has none"
        )
    if not isinstance(entries, list) or not all(
        isinstance(e, dict) and _SERVICE.fullmatch(str(e.get("name", "")))
        for e in entries
    ):
        raise RenderRefused(
            f"{directory}: {LIST_KEY} is not a list of maps with a plain `name`"
        )
    names = [e["name"] for e in entries]
    if len(set(names)) != len(names):
        raise RenderRefused(f"{directory}: {LIST_KEY} lists a service twice")
    return sorted(entries, key=lambda e: e["name"])


def refuse_second_substitution(where: str, text: str, keys: frozenset[str]) -> None:
    """Refuse a `$` in a rendered compose file that is not `$$` or `${<key>}` of `keys`.

    Raises:
        RenderRefused: Compose would interpolate something other than an env-file key.
    """
    for match in _DOLLAR.finditer(text):
        name = match.group(1)
        if match.group(0) == "$$" or name in keys:
            continue
        start = max(0, match.start() - 20)
        raise RenderRefused(
            f"{where}: Compose would substitute {match.group(0)!r} in "
            f"...{text[start : match.end() + 20]!r}...; write `$$` for a literal `$`, "
            "and `${key}` only for a secret key"
        )


def add_compose(
    package: Package,
    directory: Path,
    config: object,
    entries: object,
    names: frozenset[str],
    data_root: str,
    render: Render,
) -> frozenset[str]:
    """Render the project's include file and each service's files; return the keys they read.

    Args:
        package: Receives every file.
        directory: The component, which holds the `config` directory of service trees.
        config: The component's `compose:` value, the name of that directory.
        entries: The host's `compose_services` value.
        names: Every key of `secrets.yml`.
        data_root: The directory that holds each service's data directory.
        render: `host.py`'s renderer, which reads a service's templates.

    Raises:
        RenderRefused: The tree, the list or a compose file breaks a rule in the module docstring.
    """
    if not isinstance(config, str) or not _DIRECTORY.fullmatch(config):
        raise RenderRefused(f"{directory}: compose {config!r} is not a directory name")
    entries_sorted = services(directory, entries)
    secret = Sentinels(names)
    referenced: set[str] = set()
    compose_files: dict[str, str] = {}
    for entry in entries_sorted:
        name = entry["name"]
        tree = directory / config / name
        if not tree.is_dir():
            raise RenderRefused(
                f"{directory}: {LIST_KEY} lists {name}, which has no {tree.relative_to(directory)}"
            )
        context: dict = {"container_item": entry}
        for path in sorted(p for p in tree.rglob("*") if p.is_file()):
            relative = path.relative_to(tree).as_posix()
            if relative == f"{COMPOSE_FILE}.j2":
                continue
            plain = relative.removesuffix(".j2")
            content = (
                render(tree, relative, frozenset(), context).encode()
                if relative.endswith(".j2")
                else path.read_bytes()
            )
            _install(package, directory, f"{COMPOSE_ROOT}/{name}/{plain}", content)
            stem = PurePosixPath(plain).stem
            var = f"{name.replace('-', '_')}_{stem}_sha256"
            context[var] = hashlib.sha256(content).hexdigest()
        raw = render(tree, f"{COMPOSE_FILE}.j2", names, context)
        dst = f"{COMPOSE_ROOT}/{name}/{COMPOSE_FILE}"
        _install(package, directory, dst, secret.resolve(raw).encode())
        referenced |= secret.key_set({dst: raw})
        compose_files[dst] = secret.resolve(raw)
    for dst, text in compose_files.items():
        refuse_second_substitution(dst, text, frozenset(referenced))
    includes = ["include:"] if entries_sorted else ["include: []"]
    for entry in entries_sorted:
        name = entry["name"]
        includes += [
            f"  - path: {COMPOSE_ROOT}/{name}/{COMPOSE_FILE}",
            f"    project_directory: {data_root}/{name}",
        ]
        if referenced:
            includes.append(f"    env_file: {ENV_PATH}")
    project = "name: homelab\n" + "\n".join(includes) + "\n"
    _install(package, directory, f"{COMPOSE_ROOT}/{COMPOSE_FILE}", project.encode())
    return frozenset(referenced)


def check_env_output(
    package: Package, directory: Path, referenced: frozenset[str]
) -> None:
    """Refuse a project whose `compose.env` output does not carry exactly the keys it reads.

    Raises:
        RenderRefused: The component declares no `format: dotenv` output at `compose.env`, or
            its `keys` differ from the keys the compose files read.
    """
    rows = [
        o
        for o in package.outputs
        if o["destination"] == str(ENV_PATH).lstrip("/") and o.get("format") == "dotenv"
    ]
    declared = frozenset(rows[0]["keys"]) if rows else frozenset()
    if declared != referenced:
        raise RenderRefused(
            f"{directory}: the compose files read {sorted(referenced)} and the `format: dotenv` "
            f"output at {ENV_PATH} declares {sorted(declared)} (read and undeclared "
            f"{sorted(referenced - declared)}, declared and unread {sorted(declared - referenced)})"
        )


def inputs_sha256(package: Package, extras: Iterable[str] = INPUT_EXTRAS) -> str:
    """The sha256 over the project's files: each path, mode and content, sorted by path."""
    digest = hashlib.sha256()
    for dst, (content, mode) in sorted(package.files.items()):
        if PurePosixPath(dst).is_relative_to(COMPOSE_ROOT) or dst in extras:
            digest.update(f"{dst}\0{mode:o}\0{len(content)}\0".encode())
            digest.update(content)
    return digest.hexdigest()


def external_networks(package: Package) -> list[str]:
    """The sorted names of the networks the service compose files declare `external: true`.

    A network's `name:` wins over its key, as it does for Compose.
    """
    names = set()
    for dst, (content, _mode) in package.files.items():
        path = PurePosixPath(dst)
        if not (path.is_relative_to(COMPOSE_ROOT) and path.name == COMPOSE_FILE):
            continue
        networks = (yaml_fast.safe_load(content.decode()) or {}).get("networks") or {}
        for key, spec in networks.items():
            if isinstance(spec, dict) and spec.get("external") is True:
                names.add(str(spec.get("name", key)))
    return sorted(names)


def deferred_last(files: list) -> list:
    """`files` with the `inputs_sha256: true` entries moved last, in their listed order."""
    return sorted(
        files, key=lambda e: isinstance(e, dict) and e.get("inputs_sha256") is True
    )


def inputs_context(package: Package, directory: Path, entry: dict, ctx: dict) -> dict:
    """`ctx` with the project's hash and external networks when `entry` is marked `inputs_sha256`.

    Raises:
        RenderRefused: The marked file installs under the compose directory, so it would be
            among its own inputs.
    """
    if entry.get("inputs_sha256") is not True:
        return ctx
    if PurePosixPath(str(entry.get("dst"))).is_relative_to(COMPOSE_ROOT):
        raise RenderRefused(
            f"{directory}: {entry.get('dst')} reads {INPUTS_VAR} and installs under {COMPOSE_ROOT}"
        )
    return {
        **ctx,
        INPUTS_VAR: inputs_sha256(package),
        NETWORKS_VAR: external_networks(package),
    }


def _install(package: Package, directory: Path, dst: str, content: bytes) -> None:
    if dst in package.files:
        raise RenderRefused(f"{package.name}: two files install {dst} ({directory})")
    package.files[dst] = (content, 0o644)
