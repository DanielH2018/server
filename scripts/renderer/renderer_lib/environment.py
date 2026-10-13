"""The strict Jinja environment the renderer renders templates and task expressions in.

`cluster.render_tag` renders a role's templates here, and `task_inputs.derived_facts`
evaluates the expressions a role's `set_fact` tasks hold. Both read one environment, so a
template and a task expression see the same filters and the same `lookup`.

Typical usage example:

    env = renderer_env(K8S_ROLES / "nut" / "templates", ctx)
    text = render_file(env, "deployment.yaml.j2", ctx)
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import warnings
from pathlib import Path
from typing import cast

import jinja2
from jinja2 import pass_context
from jinja2.runtime import Context

from lib.ansible_jinja_env import template_env
from lib.k8s_yaml import make_lookup
from lib.render_guard import StrictChainableUndefined, pad_to_source_newlines


class RenderError(RuntimeError):
    """A template that failed to render; nothing has been written."""


def renderer_env(templates: Path, ctx: dict | None = None) -> jinja2.Environment:
    """The strict environment a role's templates render in: its own directory, then the shared one.

    The template census in `scripts/renderer/tests/` parses every template through this
    environment, so a filter or test the renderer lacks fails there before a render meets it.
    With `ctx`, the environment's `lookup` reads files and renders templates against it.

    Unlike the render guards' environment, it drops a template's last newline, as
    ansible-core's `AnsibleEnvironment` does, and `render_file` adds back what the template
    module adds back. So an `{% include %}` loses its last newline, as it does under Ansible.
    """
    env = template_env(templates, undefined_cls=StrictChainableUndefined)
    env.keep_trailing_newline = False
    if ctx is not None:
        env.globals["lookup"] = _template_lookup(env, ctx)
    return env


def render_file(env: jinja2.Environment, name: str, variables: dict) -> str:
    """Template `name` on `env`'s loader, rendered to the text the `template` module writes."""
    source, _, _ = cast(jinja2.BaseLoader, env.loader).get_source(env, name)
    return pad_to_source_newlines(source, env.get_template(name).render(variables))


def _template_lookup(env: jinja2.Environment, ctx: dict):
    """`make_lookup`, with `template` rendered through `env` and the calling template's variables.

    The labelling wrapper calls `lookup('ansible.builtin.template', manifests_label_src)`, and
    the inner template reads the task's loop variable (`manifests_claim`). So the lookup renders
    with the caller's variables, through the same strict environment and loader, where
    `make_lookup` renders with the role context and a `StubUndefined` environment.

    A relative name resolves on the loader's search path, as Ansible's lookup finds a bare name
    in the role's `templates/`. An absolute path outside it, such as another role's Dockerfile
    that image-builder's build context embeds, renders from its own text.
    """
    base = make_lookup(ctx)

    @pass_context
    def lookup(context: Context, kind: str, *args, **kwargs) -> object:
        kind = kind.removeprefix("ansible.builtin.")
        if kind != "template":
            return base(context, kind, *args, **kwargs)
        # Not rstripped: ansible-core's lookup pads a render's trailing newlines as the
        # template module does, and image-builder's content tag hashes them.
        return _lookup_render(env, Path(args[0]), context.get_all())

    return lookup


def _lookup_render(env: jinja2.Environment, path: Path, variables: dict) -> str:
    """`lookup('template', path)`, with the template found as Ansible's lookup finds it."""
    if not path.is_absolute():
        return render_file(env, path.as_posix(), variables)
    try:
        name = loader_name(env, path)
    except RenderError:
        source = path.read_text()
        return pad_to_source_newlines(source, env.from_string(source).render(variables))
    return render_file(env, name, variables)


def loader_name(env: jinja2.Environment, path: Path) -> str:
    """`path` as a name `env`'s loader resolves, relative to the first search path holding it.

    Raises:
        RenderError: `path` lies under none of the loader's search paths.
    """
    for root in cast(jinja2.FileSystemLoader, env.loader).searchpath:
        if path.is_relative_to(root):
            return path.relative_to(root).as_posix()
    raise RenderError(f"{path} is outside the role's templates and ansible/templates/")


def evaluate(env: jinja2.Environment, expression: str, variables: dict) -> object:
    """A task's templated value, as Ansible's templar returns it for a `set_fact`.

    A value that is one `{{ ... }}` expression and nothing else keeps its native type, so a
    dict stays a dict. Any other value renders to a string.
    """
    inner = expression[2:-2]
    # Jinja warns on a string literal holding an escape Python does not know, such as the
    # `'\.json$'` regex uptime-kuma's `kuma_declarations` passes to `regex_replace`. Ansible
    # parses it with the same lexer and only prints that warning.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", ".*invalid escape sequence", DeprecationWarning
        )
        if (
            expression.startswith("{{")
            and expression.endswith("}}")
            and "{{" not in inner
        ):
            return env.compile_expression(inner, undefined_to_none=False)(**variables)
        return pad_to_source_newlines(
            expression, env.from_string(expression).render(variables)
        )
