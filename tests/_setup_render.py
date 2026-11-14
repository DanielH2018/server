"""Render every host component template, for tests that need to assert on the OUTPUT.

`_k8s_render` reaches only roles with a `containers_list` entry. A census that has to span the
planes — which templates render the FSD-capable `nut_monitor_password` login — could otherwise
only scan template SOURCE, and a source scan cannot follow an alias: a template that reads the
secret through a second variable holds it while the scan sees nothing (#3183).

Each template renders from its OWN component's context, the one `validate/setup_templates.py`
gives it: `lib.render_context` with the host renderer's `inventory_hostname` on top.
`overrides` go on top as well, so a guard can set a secret to a sentinel and look for it, and a
default of `"{{ some_secret }}"` carries the sentinel rather than the secret's stub.

Secret values a caller does not override render as `STUB` (the `DECIDED:` rule in
`lib/render_guard.py`).
"""

from functools import cache
from pathlib import Path, PurePosixPath

from lib.render_context import render_context
from renderer.renderer_lib import host as host_mode
from renderer.renderer_lib.host_outputs import COMPOSE_ROOT, TEMPLATE_ROOT
from renderer.renderer_lib.host_package import Package
from validate.setup_templates import (
    HOST_RENDER_CONTEXT,
    COMPONENTS,
    component_context,
    standalone_templates,
)
from validate.validate_lib.shipped_render import render_shipped


def role_context(role_dir: Path, overrides: dict | None = None) -> dict:
    """The resolved context one component's templates render with, `overrides` on top.

    A default that will not expand is left out, so it renders as `STUB`: some defaults call a
    filter `lib.render_context`'s light environment does not carry, which `_resolve`
    there names.
    """
    return render_context(
        role_dir, overrides={**HOST_RENDER_CONTEXT, **(overrides or {})}
    )


def render_setup_texts(
    overrides: dict | None = None, components: Path = COMPONENTS
) -> tuple[tuple[str, str, str], ...]:
    """(component, template name, rendered TEXT) for every template under `components`, uncached.

    The template set is `validate/setup_templates.py:standalone_templates`, the gate's own set
    less the partials a sibling template loads, so each text is one the package renders. Raises
    on a render failure rather than skipping it: a template that stopped rendering would
    otherwise drop out of every census built on this. `components` is a parameter so a test can point it at a `tmp_path` tree.
    """
    contexts: dict[Path, dict] = {}
    texts = []
    for tpl in standalone_templates(components):
        directory = tpl.parents[1]
        if directory not in contexts:
            contexts[directory] = component_context(
                directory, {**HOST_RENDER_CONTEXT, **(overrides or {})}
            )
        rendered, err = render_shipped(tpl, contexts[directory])
        if rendered is None:
            raise AssertionError(f"{directory.name}/{tpl.name}: {err}")
        texts.append((directory.name, tpl.name, rendered))
    return tuple(texts)


_TEXTS: tuple[tuple[str, str, str], ...] | None = None


def rendered_setup_texts() -> tuple[tuple[str, str, str], ...]:
    """`render_setup_texts()` at inventory values, rendered at most once per process.

    The host component counterpart of `_k8s_render.rendered_texts`. Cached because the render is a
    pure function of the repo tree, which no test writes to.
    """
    global _TEXTS
    if _TEXTS is None:
        _TEXTS = render_setup_texts()
    return _TEXTS


PI_COMPOSE = "pi-compose"


def pi_compose_texts(
    package: Package, overrides: dict | None = None
) -> tuple[tuple[str, str, str], ...]:
    """(component, file name, TEXT) for every Compose project file a host package ships.

    pi-compose keeps its templates outside a `templates/` directory, so `render_setup_texts`
    never reaches them; this reads them from the package the host renderer built instead. A
    service's files are labelled `pi-compose/<service>`, and the project file and the
    `compose.env` secret template `pi-compose`.

    Host mode renders each secret a compose file reads, alias or not, as `${<key>}`. Each key of
    `overrides` has that reference replaced by its value, so a census that sets a secret to a
    sentinel finds the sentinel here as it does on the other planes.
    """
    env_root = TEMPLATE_ROOT / COMPOSE_ROOT.relative_to("/")
    texts = []
    for dst, (content, _) in sorted(package.files.items()):
        path = PurePosixPath(dst)
        root = next(
            (r for r in (COMPOSE_ROOT, env_root) if path.is_relative_to(r)), None
        )
        if root is None:
            continue
        parts = path.relative_to(root).parts
        component = "/".join((PI_COMPOSE, *parts[:-1]))
        text = content.decode()
        for key, value in (overrides or {}).items():
            text = text.replace(f"${{{key}}}", str(value))
        texts.append((component, parts[-1], text))
    return tuple(texts)


@cache
def pi_package() -> Package:
    """daniel-pi's host package at inventory values, rendered at most once per process."""
    return host_mode.render_host("daniel-pi")


# The component that took over the claude_code setup role's sources (#6773).
CLAUDE_CODE = COMPONENTS / "claude-code"


def render_component_text(
    component: str, template: str, overrides: dict | None = None
) -> str:
    """One template of a host component's `templates/`, rendered as text.

    The context is the component's `defaults/main.yml` beneath the inventory, with `overrides`
    on top. It leaves out the component.yml
    `defaults:`, so a sibling unit name renders as its defaults/main.yml value. The loader is
    rooted at the component, so a template imports a sibling as `templates/<name>`, the way
    the host renderer loads it.

    Fails naming the template when the component does not ship it.
    """
    directory = COMPONENTS / component
    name = f"templates/{template}"
    assert (directory / name).is_file(), (
        f"no such component template: {directory / name}"
    )
    rendered, err = render_shipped(directory / name, role_context(directory, overrides))
    if rendered is None:
        raise AssertionError(f"{component}/{name}: {err}")
    return rendered


@cache
def rendered_component_text(component: str, template: str) -> str:
    """`render_component_text` at inventory values, rendered at most once per process."""
    return render_component_text(component, template)
