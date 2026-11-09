"""The Jinja environment a test renders a task expression or a macro in.

Kept apart from `_helpers.py` because it imports the renderer's filter set, and with it every
filter plugin, which the prek hooks whose entries import `_helpers.py` would otherwise have to
run on.
"""

from jinja2 import FileSystemLoader
from jinja2.nativetypes import NativeEnvironment

from lib.ansible_jinja_env import register_ansible_filters
from lib.repo_paths import ANSIBLE
from renderer.renderer_lib import jinja_filters


def jinja_env() -> NativeEnvironment:
    """A NativeEnvironment carrying the renderer's filters and tests.

    `NativeEnvironment` returns real Python objects where a plain Jinja2 environment would hand
    back a string repr — needed by any expression under test that produces a structure rather
    than text. The filters and tests are `lib.ansible_jinja_env`'s set, the one the renderer
    registers, so an expression renders against the code a render runs.
    """
    # The loader is what lets a template under test `{% import %}` a shared macro from
    # ansible/templates/. Without one, Jinja raises `TypeError: no loader for this environment
    # specified` at render time — an error naming the environment rather than the macro, which
    # reads as a broken helper rather than a template that reaches outside itself.
    env = NativeEnvironment(loader=FileSystemLoader(str(ANSIBLE / "templates")))
    register_ansible_filters(env)
    env.filters["to_json"] = jinja_filters.to_json
    return env


def render_expr(expression: str, **context):
    """Render a Jinja expression through `jinja_env()`."""
    return jinja_env().from_string(expression).render(**context)
