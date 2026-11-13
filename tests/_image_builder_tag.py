"""Evaluate a templated value through the renderer's environment, as an image-builder task would.

`CONTENT_TAG_EXPRESSION` is k8s/image-builder's content-tag rule as a task evaluates it, and
`derived.content_tag` and `ci_images.content_tag` are held equal to it. Those modules restate
the rule in Python; this evaluates the role's own expression.
`scripts/deploy_tools/tests/test_ci_images.py` holds one tag as the literal ansible-core
2.22 computed.
"""

from pathlib import Path

from _helpers import K8S_ROLES
from renderer.renderer_lib.environment import evaluate, renderer_env

IMAGE_BUILDER = K8S_ROLES / "image-builder"
CONTEXT_DATA_EXPRESSION = (
    "{{ (lookup('template', 'context-configmap.yaml.j2') | from_yaml)['data'] }}"
)
CONTENT_TAG_EXPRESSION = (
    "sha-{{ ((lookup('template', 'context-configmap.yaml.j2') | from_yaml)['data']\n"
    "        | dictsort | to_json | hash('sha256'))[:12] }}"
)


def expression_value(expression: str, variables: dict, role: Path) -> object:
    """`expression` as a task in `role` evaluates it, with `variables` as the task's variables.

    A bare template name in a lookup resolves in `<role>/templates/`, as a role task finds it.
    """
    env = renderer_env(role / "templates", variables)
    return evaluate(env, expression, variables)


def expression_content_tag(variables: dict, role: Path = IMAGE_BUILDER) -> str:
    """image-builder's content tag for a build with `variables`, as a deploy computes it.

    Point `role` at a copy of image-builder to vary its templates.
    """
    return str(expression_value(CONTENT_TAG_EXPRESSION, variables, role)).strip()
