"""The keys a `package.yml` and a `component.yml` may carry, and the one loader that checks them.

`ComponentFile` and `PackageFile` name every top-level key; `load_spec` refuses a file with any
other, naming it, so a misspelt key fails the render rather than being ignored. Each is a
`key_models.keys_model`, so it types every value `Any`, and `load_spec` returns the YAML mapping
itself: the render code that reads a key refuses a wrong shape (`enable` "is not a map").

Typical usage example:

    spec = load_spec(Path("hosts/components/x/component.yml"), ComponentFile)
"""

from pathlib import Path

import pydantic

from lib import yaml_fast
from renderer.renderer_lib.cluster import RenderRefused
from renderer.renderer_lib.host_arms import ARM_KEYS
from renderer.renderer_lib.key_models import key_errors, keys_model


# The keys a package and a component share: what one directory installs.
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
    *sorted(ARM_KEYS),
)
_PACKAGE_KEYS = ("components", "description", "maintainer_scripts", "tools", "tools_on")

# A `hosts/components/<name>/component.yml`.
ComponentFile = keys_model("ComponentFile", (*_UNIT_KEYS, "compose"))
# A `hosts/<host>/package.yml` or `hosts/packages/<pkg>/package.yml`.
PackageFile = keys_model("PackageFile", (*_UNIT_KEYS, *_PACKAGE_KEYS))


def load_spec(path: Path, model: type[pydantic.BaseModel]) -> dict:
    """`path`'s YAML mapping, refused unless `model` names every top-level key.

    Raises:
        RenderRefused: The file is not a mapping, or carries a key `model` does not name.
    """
    spec = yaml_fast.safe_load(path.read_text()) or {}
    if not isinstance(spec, dict):
        raise RenderRefused(f"{path} is not a mapping")
    unknown = key_errors(model, spec).unknown
    if unknown:
        raise RenderRefused(f"{path} has unknown keys {unknown}")
    return spec
