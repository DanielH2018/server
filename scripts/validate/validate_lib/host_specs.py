"""The host package specs the validators read, each parsed once at its load.

`load_package` reads a `package.yml` and `load_component` a `component.yml`, through the
renderer's own `load_spec`, so a validator refuses a malformed file exactly as a render does.
Both are cached, because the shell, setup-plane, unit and asset-pin validators read the same
files in one run. `load_nfpm` reads a rendered `nfpm.yaml`; it is not cached, because a render
directory is rewritten between runs in one process.

Each load is the one place a malformed file is refused. Every validator under
`scripts/validate/` takes the parsed spec and checks no key's shape itself.

Typical usage example:

    components = load_package(HOSTS / "daniel-pi" / "package.yml").components
"""

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import MappingProxyType

from lib import yaml_fast
from renderer.renderer_lib.cluster import RenderRefused
from renderer.renderer_lib.host_spec import ComponentFile, PackageFile, load_spec


@dataclass(frozen=True)
class PackageSpec:
    """One `package.yml`.

    Attributes:
        name: The package's directory name, which is its name.
        components: The components it lists, in file order.
    """

    name: str
    components: tuple[str, ...]


@dataclass(frozen=True)
class ComponentSpec:
    """One `component.yml`.

    Attributes:
        name: The component's directory name.
        borrows: The component named by `component:`, whose templates this one renders.
        defaults: Its `defaults:` map, read-only because the load is cached.
        output_keys: Every secret key its `outputs:` entries declare.
        text: The file as written, for a reader of its comments.
    """

    name: str
    borrows: str | None
    defaults: Mapping[str, object]
    output_keys: frozenset[str]
    text: str


@dataclass(frozen=True)
class NfpmSpec:
    """One rendered `nfpm.yaml`.

    Attributes:
        name: The package name.
        contents: Its content entries, each a mapping with at least `dst`.
    """

    name: str
    contents: tuple[Mapping[str, object], ...]


def _string_list(raw: object, path: Path, key: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise RenderRefused(f"{path}: `{key}` must be a list of names")
    return tuple(raw)


@cache
def load_package(path: Path) -> PackageSpec:
    """The `package.yml` at `path`.

    Raises:
        RenderRefused: The file is not a mapping, carries a key the renderer does not know,
            or its `components` is not a list of names.
    """
    raw = load_spec(path, PackageFile)
    return PackageSpec(
        name=path.parent.name,
        components=_string_list(raw.get("components"), path, "components"),
    )


@cache
def load_component(path: Path) -> ComponentSpec:
    """The `component.yml` at `path`.

    Raises:
        RenderRefused: The file is not a mapping, carries a key the renderer does not know,
            its `component` is not a name, its `defaults` is not a mapping, or an `outputs`
            entry is not a mapping whose `keys` is a list of names.
    """
    raw = load_spec(path, ComponentFile)
    borrows = raw.get("component")
    if borrows is not None and not isinstance(borrows, str):
        raise RenderRefused(f"{path}: `component` must be a component name")
    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise RenderRefused(f"{path}: `defaults` must be a mapping")
    outputs = raw.get("outputs") or []
    if not isinstance(outputs, list) or not all(isinstance(o, dict) for o in outputs):
        raise RenderRefused(f"{path}: `outputs` must be a list of mappings")
    return ComponentSpec(
        name=path.parent.name,
        borrows=borrows,
        defaults=MappingProxyType(defaults),
        output_keys=frozenset(
            key
            for output in outputs
            for key in _string_list(output.get("keys"), path, "outputs[].keys")
        ),
        text=path.read_text(),
    )


def load_nfpm(path: Path) -> NfpmSpec:
    """The rendered `nfpm.yaml` at `path`.

    Raises:
        ValueError: The file is not a mapping, has no string `name`, or its `contents` is
            not a list of mappings each carrying a `dst`.
    """
    raw = yaml_fast.safe_load(path.read_text())
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
        raise ValueError(f"{path} is not an nfpm spec with a `name`")
    contents = raw.get("contents") or []
    if not isinstance(contents, list) or not all(
        isinstance(entry, dict) and "dst" in entry for entry in contents
    ):
        raise ValueError(f"{path}: `contents` must be a list of entries with a `dst`")
    return NfpmSpec(name=raw["name"], contents=tuple(contents))
