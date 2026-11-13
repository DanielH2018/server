"""Render a template to the bytes the renderer ships, for the guards that lint the result.

The renderer renders a host component through `renderer_lib.environment.strict_env` and
`render_file`, and a k8s role's embedded script through the same pair. That environment drops
a template's last newline and `render_file` pads it as Ansible's template module does, so an
`{% include %}` loses its last newline. `template_env` alone keeps it, so a guard rendering
through it can pass on text the package never ships.

The setup, shell, unit and compose guards render here. Each passes its own undefined class,
because a guard has no SOPS secrets and must not abort on the first one.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from pathlib import Path

from jinja2 import Undefined

from lib.k8s_yaml import to_json_stub
from lib.render_guard import StubUndefined
from renderer.renderer_lib.environment import render_file, strict_env

__all__ = ["render_shipped", "render_shipped_text", "template_loader"]


def template_loader(tpl: Path) -> tuple[Path, str]:
    """The directory `tpl`'s environment is rooted at, and the name it loads by.

    A host component template loads from the component, as the host renderer loads it, so its
    `templates/<name>` imports resolve. Any other template loads from its own directory.
    """
    if (tpl.parents[1] / "component.yml").is_file():
        return tpl.parents[1], f"templates/{tpl.name}"
    return tpl.parent, tpl.name


def render_shipped(
    tpl: Path, ctx: dict, undefined_cls: type[Undefined] = StubUndefined
) -> tuple[str | None, str | None]:
    """Render `tpl` to the bytes the renderer ships, with `undefined_cls` for missing names.

    `ctx` reaches the template as variables only, as in `renderer_lib/host.py`. The renderer's
    `to_json` refuses a stubbed secret, so the environment carries `to_json_stub`, which writes
    the same JSON for plain data.

    Returns:
        `(rendered, None)` on success, or `(None, "render error: <Type>: <msg>")`.
    """
    root, name = template_loader(tpl)
    env = strict_env(root, undefined_cls=undefined_cls)
    env.filters["to_json"] = to_json_stub
    try:
        return render_file(env, name, ctx), None
    except Exception as exc:
        return None, f"render error: {type(exc).__name__}: {exc}"


def render_shipped_text(tpl: Path, ctx: dict) -> str:
    """`render_shipped` with `StubUndefined`, RAISING RuntimeError if `tpl` will not render.

    Raises:
        RuntimeError: Carrying `render_shipped`'s message, when the render fails.
    """
    rendered, err = render_shipped(tpl, ctx)
    if rendered is None:
        raise RuntimeError(err)
    return rendered
