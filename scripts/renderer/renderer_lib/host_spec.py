"""The keys a `package.yml` and a `component.yml` may carry, and the one loader that checks them.

`ComponentFile` and `PackageFile` name every top-level key; `load_spec` refuses a file with any
other, naming it, so a misspelt key fails the render rather than being ignored. The models type
each value `Any`: the render code that reads a key refuses a wrong shape with a message naming
what the key means (`enable` "is not a map"), which a type error raised here first would replace.
`load_spec` returns the YAML mapping itself, never the model's dump, so no default reaches a reader.

Typical usage example:

    spec = load_spec(Path("hosts/components/x/component.yml"), ComponentFile)
"""

from pathlib import Path
from typing import Any

import pydantic

from lib import yaml_fast
from renderer.renderer_lib.cluster import RenderRefused


# The keys a package and a component share: what one directory installs. The last four are
# `host_arms.ARM_KEYS`.
_UNIT_KEYS = (
    "component",
    "credentials",
    "defaults",
    "depends",
    "enable",
    "files",
    "legacy",
    "outputs",
    "units",
    "ufw",
    "libvirt",
    "users",
    "lines",
)
_PACKAGE_KEYS = ("components", "description", "maintainer_scripts", "tools", "tools_on")


def _keys_model(name: str, keys: tuple[str, ...]) -> type[pydantic.BaseModel]:
    """A model taking any value for each of `keys` and refusing every other key."""
    fields: dict[str, Any] = {key: (Any, None) for key in keys}
    return pydantic.create_model(
        name, __config__=pydantic.ConfigDict(extra="forbid"), **fields
    )


# A `hosts/components/<name>/component.yml`.
ComponentFile = _keys_model("ComponentFile", (*_UNIT_KEYS, "compose"))
# A `hosts/<host>/package.yml` or `hosts/packages/<pkg>/package.yml`.
PackageFile = _keys_model("PackageFile", (*_UNIT_KEYS, *_PACKAGE_KEYS))


def load_spec(path: Path, model: type[pydantic.BaseModel]) -> dict:
    """`path`'s YAML mapping, refused unless `model` names every top-level key.

    Raises:
        RenderRefused: The file is not a mapping, or carries a key `model` does not name.
    """
    spec = yaml_fast.safe_load(path.read_text()) or {}
    if not isinstance(spec, dict):
        raise RenderRefused(f"{path} is not a mapping")
    try:
        model.model_validate(spec)
    except pydantic.ValidationError as exc:
        unknown = sorted(str(e["loc"][0]) for e in exc.errors())
        raise RenderRefused(f"{path} has unknown keys {unknown}") from exc
    return spec
