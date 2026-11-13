"""The render metadata and image builds a role's `defaults/main.yml` gives the renderer.

`include_vars` reads a role's render metadata, and `image_builder_sites` reads its builds. No
value comes from a role's `tasks/`.

`derived_facts` computes the one value a template reads that no file states,
`k8s_built_image_tags`, with each tag by `derived.content_tag`.
A build's paths come from `role_file()`, so a lookup reads the calling role's files.

No value a template reads comes from live state, so the renderer pins no fact. Target slice
R3 removed the last live reads, and R3's live-read census in `scripts/renderer/tests/` holds
the tree to that.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import copy
import functools
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import jinja2

from lib import yaml_fast
from lib.k8s_roles import RENDER_KEYS
from lib.repo_paths import K8S_ROLES
from renderer.renderer_lib.derived import content_tag
from renderer.renderer_lib.environment import evaluate, renderer_env


IMAGE_BUILDER = K8S_ROLES / "image-builder"
BUILT_IMAGE_TAGS = "k8s_built_image_tags"
# The role default listing the images a role builds, one mapping of `image_builder_*` vars per
# build, with an optional `when`.
IMAGE_BUILDS = "image_builder_builds"
# The `data` the content tag hashes.
_CONTEXT_DATA = (
    "{{ (lookup('template', 'context-configmap.yaml.j2') | from_yaml)['data'] }}"
)


def _parsed(path: Path) -> object:
    """`path` parsed as YAML, a fresh copy on each call so a caller cannot alter the cache."""
    return copy.deepcopy(_parsed_text(path.read_text()))


# Keyed on the text rather than the path, as `render_guard._parsed_mapping` is, so an edit
# between two renders reaches the second one. A render reads a role's defaults more than once
# (#5900).
@functools.lru_cache(maxsize=512)
def _parsed_text(text: str) -> object:
    return yaml_fast.safe_load(text)


def _defaults(role_dir: Path) -> dict:
    """`role_dir`'s `defaults/main.yml`, unresolved.

    Raises:
        ValueError: The role has no such file.
    """
    path = role_dir / "defaults" / "main.yml"
    if not path.is_file():
        raise ValueError(f"{role_dir.name} has no defaults/main.yml")
    return cast(dict, _parsed(path)) or {}


def render_defaults(role_dir: Path) -> dict:
    """The `RENDER_KEYS` that `role_dir`'s `defaults/main.yml` sets, unresolved."""
    return {k: v for k, v in _defaults(role_dir).items() if k in RENDER_KEYS}


def include_vars(role_dir: Path) -> dict:
    """`role_dir`'s render metadata, as `render_defaults` reads it.

    The values are read as written, so a Jinja value is still a string for the caller to
    resolve.

    Raises:
        ValueError: The role has no `defaults/main.yml`.
    """
    return render_defaults(role_dir)


@dataclass(frozen=True)
class BuildSite:
    """One image a role builds, as its `image_builder_builds` writes it.

    Attributes:
        vars: The build's `image_builder_*` vars, unresolved, so a value or a key may still
            hold Jinja.
        when: The build's `when:` conditions, empty for an unconditional build.
    """

    vars: dict
    when: tuple[str, ...] = ()


def _conditions(when: object) -> tuple[str, ...]:
    """A `when:` value as the tuple of conditions it holds."""
    if not when:
        return ()
    return (when,) if isinstance(when, str) else tuple(cast(list, when))


def image_builder_sites(role_dir: Path) -> list[BuildSite]:
    """`role_dir`'s builds, as its `image_builder_builds` lists them.

    Raises:
        ValueError: The role has no `defaults/main.yml`.
    """
    return [
        BuildSite(
            {k: v for k, v in build.items() if k != "when"},
            _conditions(build.get("when")),
        )
        for build in _defaults(role_dir).get(IMAGE_BUILDS) or []
    ]


def _template(env: jinja2.Environment, value: object, variables: dict) -> object:
    """`value` with every string templated, the keys of a mapping included, as Ansible does."""
    if isinstance(value, str):
        return evaluate(env, value, variables) if "{" in value else value
    if isinstance(value, dict):
        return {
            _template(env, k, variables): _template(env, v, variables)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_template(env, v, variables) for v in value]
    return value


def site_variables(site: BuildSite, ctx: dict) -> dict:
    """The variables image-builder's tasks see for `site` in a role whose context is `ctx`.

    image-builder's own defaults sit under the calling role's variables, and the include's
    `vars:` over both. `role_path` stays the calling role's, so a build's `role_file()` paths
    name that role's files.
    """
    defaults = cast(dict, _parsed(IMAGE_BUILDER / "defaults" / "main.yml"))
    variables = {**defaults, **ctx}
    env = renderer_env(IMAGE_BUILDER / "templates", variables)
    return {**variables, **cast(dict, _template(env, site.vars, variables))}


def context_data(variables: dict) -> dict:
    """The `data` of image-builder's context ConfigMap for a build with `variables`.

    Args:
        variables: The build's variables, as `site_variables` returns them.
    """
    env = renderer_env(IMAGE_BUILDER / "templates", variables)
    return cast(dict, evaluate(env, _CONTEXT_DATA, variables))


@dataclass(frozen=True)
class ImageContext:
    """One build's rendered context and its content tag.

    Attributes:
        data: The `data` of image-builder's context ConfigMap, each file name mapped to its
            text, parsed as the content tag's expression parses it. It holds every byte the
            build reads.
        tag: `derived.content_tag` over `data`.
    """

    data: dict[str, str]
    tag: str


def image_contexts(role_dir: Path, ctx: dict) -> dict[str, ImageContext]:
    """Each image `role_dir` builds, mapped to its context and content tag.

    A build whose `when:` is false is left out, as Ansible skips it. A file written from
    `data` hashes to `tag`.
    """
    built = {}
    for site in image_builder_sites(role_dir):
        variables = site_variables(site, ctx)
        env = renderer_env(IMAGE_BUILDER / "templates", variables)
        if not all(env.compile_expression(cond)(**variables) for cond in site.when):
            continue
        data = context_data(variables)
        built[variables["image_builder_name"]] = ImageContext(
            {str(k): str(v) for k, v in data.items()},
            content_tag(data),
        )
    return built


def built_image_tags(role_dir: Path, ctx: dict) -> dict[str, str]:
    """`k8s_built_image_tags` after `role_dir`'s builds: each image's name mapped to its tag.

    The map starts empty for each role, and each build adds its own image, so a role that
    builds nothing reads an empty map. A build whose `when:` is false
    adds nothing, and its consumer falls back to `latest`.
    """
    return {name: c.tag for name, c in image_contexts(role_dir, ctx).items()}


def derived_facts(
    role_dir: Path, ctx: dict, built: dict[str, str] | None = None
) -> dict:
    """The values `role_dir`'s templates and defaults read that no file states.

    That is `k8s_built_image_tags`, for every role.

    Args:
        role_dir: The role directory, `ansible/roles/k8s/<tag>`.
        ctx: The role's play-time context, the stub file's variables included.
        built: `k8s_built_image_tags` as the caller already computed it from `ctx`, so a
            render's `DIR/images/tags.json` and its consumers' refs share one computation.
            None computes it here.

    Raises:
        jinja2.TemplateError: A build's context will not render.
    """
    return {
        BUILT_IMAGE_TAGS: dict(
            built if built is not None else built_image_tags(role_dir, ctx)
        )
    }
