"""Render every setup-plane template, for tests that need to assert on the OUTPUT.

`_k8s_render` reaches only roles with a `containers_list` entry, and no setup role has one, so
`roles/setup/` was out of reach of every render guard. A census that has to span the planes —
which roles render the FSD-capable `nut_monitor_password` login, where `setup/nut_host` is a
required member — could only scan template SOURCE, and a source scan cannot follow an alias:
a template that reads the secret through a second variable holds it while the scan sees
nothing (#3183).

Each template renders from its OWN role's context, the one `validate/setup_templates.py:main`
gives it: `lib.render_context` with Ansible's runtime names on top. `overrides` go on top as
well, so a guard can set a secret to a sentinel and look for it, and a default of
`"{{ some_secret }}"` carries the sentinel rather than the secret's stub.

Secret values a caller does not override render as `STUB` (the `DECIDED:` rule in
`lib/render_guard.py`).
"""

from pathlib import Path

from lib.ansible_jinja_env import template_env
from lib.render_context import render_context
from lib.render_guard import render_or_error
from validate.setup_templates import (
    ANSIBLE_RUNTIME_CONTEXT,
    SETUP,
    discover_templates,
)


def role_context(role_dir: Path, overrides: dict | None = None) -> dict:
    """The resolved context one setup role's templates render with, `overrides` on top.

    A default that will not expand is left out, so it renders as `STUB`: three setup defaults
    call a filter `lib.render_context`'s light environment does not carry, which `_resolve`
    there names.
    """
    return render_context(
        role_dir, overrides={**ANSIBLE_RUNTIME_CONTEXT, **(overrides or {})}
    )


def render_setup_texts(
    overrides: dict | None = None, setup: Path = SETUP
) -> tuple[tuple[str, str, str], ...]:
    """(role, template name, rendered TEXT) for every template under `setup`, uncached.

    The template set is `validate/setup_templates.py:discover_templates`, so what a guard here
    covers cannot drift from what that gate renders. Raises on a render failure rather than
    skipping it: a template that stopped rendering would otherwise drop out of every census
    built on this. `setup` is a parameter so a test can point it at a `tmp_path` tree.
    """
    contexts: dict[Path, dict] = {}
    texts = []
    for tpl in discover_templates(setup):
        role_dir = tpl.parents[1]
        if role_dir not in contexts:
            contexts[role_dir] = role_context(role_dir, overrides)
        rendered, err = render_or_error(
            template_env(tpl.parent), tpl.name, contexts[role_dir]
        )
        if rendered is None:
            raise AssertionError(f"setup/{role_dir.name}/{tpl.name}: {err}")
        texts.append((role_dir.name, tpl.name, rendered))
    return tuple(texts)


_TEXTS: tuple[tuple[str, str, str], ...] | None = None


def rendered_setup_texts() -> tuple[tuple[str, str, str], ...]:
    """`render_setup_texts()` at inventory values, rendered at most once per process.

    The setup-plane counterpart of `_k8s_render.rendered_texts`. Cached because the render is a
    pure function of the repo tree, which no test writes to.
    """
    global _TEXTS
    if _TEXTS is None:
        _TEXTS = render_setup_texts()
    return _TEXTS


def rendered_setup_text(role: str, template: str) -> str:
    """One entry of `rendered_setup_texts`, by role and template name.

    Fails naming the template when the set does not hold it, so a guard over a renamed
    template reads as a failure rather than as a pass over nothing.
    """
    for name, tpl, text in rendered_setup_texts():
        if (name, tpl) == (role, template):
            return text
    raise AssertionError(
        f"setup/{role}/{template} is not among the rendered setup templates: "
        f"{sorted((n, t) for n, t, _ in rendered_setup_texts())}"
    )


def render_setup_text(role: str, template: str, overrides: dict | None = None) -> str:
    """One setup template rendered as text, with `overrides` laid over its role context.

    The setup-plane counterpart of `_shell_render.render_shell_script`, and the form a guard
    needs to ask "does this template FOLLOW its variable": render once at inventory values,
    render again with the variable moved, and compare. `rendered_setup_text` answers the
    inventory-values half from the cache; this one renders a single template rather than the
    whole plane, so a guard with several overrides does not pay 90-odd renders per override.

    Fails naming the template when the role does not ship it, so a renamed template reads as a
    failure rather than as a guard over nothing.
    """
    role_dir = SETUP / role
    path = role_dir / "templates" / template
    assert path.is_file(), f"no such setup template: {path}"
    rendered, err = render_or_error(
        template_env(path.parent), path.name, role_context(role_dir, overrides)
    )
    if rendered is None:
        raise AssertionError(f"setup/{role}/{template}: {err}")
    return rendered
