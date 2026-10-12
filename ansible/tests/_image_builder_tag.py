"""Evaluate a role task's templated value through ansible-core's own `Templar`.

`test_image_builder_content_tag.py` evaluates k8s/image-builder's content-tag expression this
way, and `homelab-render`'s tests compare the renderer's derived values with the same
evaluation. Both read the expression out of the role rather than restating it.
"""

import functools
import warnings
from pathlib import Path

from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar, trust_as_template

from _helpers import ANSIBLE, load_tasks

IMAGE_BUILDER = ANSIBLE / "roles" / "k8s" / "image-builder"
CONTENT_TAG = "image_builder_content_tag"


def set_fact_expression(tasks: Path, fact: str) -> str:
    """The template the one `set_fact` in `tasks` that sets `fact` assigns, verbatim.

    Raises:
        LookupError: No `set_fact` in `tasks` sets `fact`.
    """
    for task in load_tasks(tasks):
        facts = task.get("ansible.builtin.set_fact") or {}
        if fact in facts:
            return facts[fact]
    raise LookupError(f"no set_fact assigns {fact} in {tasks}")


@functools.cache
def _init_plugin_loader() -> None:
    """Load the plugins a bare `Templar` reaches by their `ansible.builtin.` names.

    Without it `lookup('ansible.builtin.file', ...)` raises "plugin not found", and inside a
    `{% set %}` the raise is lost, so a checksum quietly hashes an empty list. A second call
    in one process warns that the finder is already configured, hence the cache and the
    filter, since another test may have loaded it first.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", "AnsibleCollectionFinder has already been")
        init_plugin_loader()


def templar_value(expression: str, variables: dict, role: Path) -> object:
    """`expression` as a task in `role` evaluates it, with `variables` as the task's variables.

    `set_basedir` is what puts `<role>/templates/` on a lookup's search path, the same place a
    role task finds a bare template name.
    """
    _init_plugin_loader()
    loader = DataLoader()
    loader.set_basedir(str(role))
    templar = Templar(loader=loader, variables=variables)
    return templar.template(trust_as_template(expression))


def templar_content_tag(variables: dict, role: Path = IMAGE_BUILDER) -> str:
    """image-builder's content tag for a build with `variables`, as a deploy computes it.

    Point `role` at a copy of image-builder to vary its templates.
    """
    expression = set_fact_expression(role / "tasks" / "main.yml", CONTENT_TAG)
    return str(templar_value(expression, variables, role)).strip()
