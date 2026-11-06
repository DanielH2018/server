"""Evaluate a templated value through ansible-core's own `Templar`.

`CONTENT_TAG_EXPRESSION` is k8s/image-builder's content-tag rule as Ansible evaluates it, and the
oracle `derived.content_tag` and `ci_images.content_tag` are held equal to.
"""

import functools
import warnings
from pathlib import Path

from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar, trust_as_template

from _helpers import ANSIBLE

IMAGE_BUILDER = ANSIBLE / "roles" / "k8s" / "image-builder"
CONTEXT_DATA_EXPRESSION = (
    "{{ (lookup('template', 'context-configmap.yaml.j2') | from_yaml)['data'] }}"
)
CONTENT_TAG_EXPRESSION = (
    "sha-{{ ((lookup('template', 'context-configmap.yaml.j2') | from_yaml)['data']\n"
    "        | dictsort | to_json | hash('sha256'))[:12] }}"
)


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
    return str(templar_value(CONTENT_TAG_EXPRESSION, variables, role)).strip()
