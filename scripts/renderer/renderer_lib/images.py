"""The image contexts a cluster render writes under `DIR/images/` (D5.4a1, C1 *Cluster output*).

Each image `k8s/image-builder` builds gets `DIR/images/<name>/`, one file per key of its
context ConfigMap's `data`, written byte for byte. `DIR/images/tags.json` maps each name to its
content tag, `sha-<12 hex>` over that `data` as `image_builder_content_tag` computes it.

The contexts render from every role that includes `k8s/image-builder`, whatever tags the render
names, so a `--role` render still writes all ten. Each renders with its role's play context and
the include's `image_builder_*` vars (`task_inputs.image_contexts`). The consumer pass reads the
same `ImageContext` tags (`render_tag`'s `built`), so a consumer's ref and `tags.json` come from
one computation. `ref_mismatches` holds them equal, and `paths_into_images` holds that no
Kustomization reaches `DIR/images/`.

No Kustomization applies these files. D5.4a2's `images` job builds and pushes them, and D5.4b
points the flux-owned consumers at GHCR. The plan is
docs/target-architecture/deploy/d5-bulk-move.md, *Images built by CI (D5.4)*.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import json
import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import jinja2
from ansible.errors import AnsibleError

from lib import yaml_fast
from lib.ansible_inventory import K8S_HOST
from lib.repo_paths import K8S_ROLES
from renderer.renderer_lib.bundle import CLUSTER_DIR, FLUX_DIR, KUSTOMIZATION
from renderer.renderer_lib.cluster import RenderedTag, RenderRefused, tag_context
from renderer.renderer_lib.environment import RenderError
from renderer.renderer_lib.task_inputs import image_builder_sites, image_contexts

IMAGES_DIR = "images"
TAGS_FILE = f"{IMAGES_DIR}/tags.json"


@dataclass(frozen=True)
class BuiltImage:
    """One image a role builds, as a render computed it.

    Attributes:
        role: The role whose tasks include `k8s/image-builder` for it.
        data: The context's files, each name mapped to its text.
        tag: The content tag over `data`.
    """

    role: str
    data: dict[str, str]
    tag: str


def builder_roles(roles: Path = K8S_ROLES) -> list[str]:
    """The sorted roles whose tasks include `k8s/image-builder`."""
    return sorted(
        role.name
        for role in roles.iterdir()
        if role.is_dir() and image_builder_sites(role)
    )


def render_images(overrides: dict, host: str = K8S_HOST) -> dict[str, BuiltImage]:
    """Every built image's context and tag, rendered with `overrides` over each role's context.

    Raises:
        RenderError: A context fails to render, or two builds name the same image.
    """
    images: dict[str, BuiltImage] = {}
    for role in builder_roles():
        try:
            ctx = tag_context(K8S_ROLES / role, role, overrides, host)
            built = image_contexts(K8S_ROLES / role, ctx)
        except (ValueError, jinja2.TemplateError, AnsibleError) as exc:
            raise RenderError(f"{role}'s image context: {exc}") from exc
        for name, context in built.items():
            if name in images:
                raise RenderError(
                    f"{role} and {images[name].role} both build the image {name}"
                )
            images[name] = BuiltImage(role, context.data, context.tag)
    return images


def role_tags(images: Mapping[str, BuiltImage], role: str) -> dict[str, str]:
    """`k8s_built_image_tags` for `role`: the images it builds, each mapped to its tag."""
    return {name: image.tag for name, image in images.items() if image.role == role}


def image_files(images: Mapping[str, BuiltImage]) -> dict[str, str]:
    """`DIR/images/`'s files, keyed by path under `DIR`: each context and `tags.json`."""
    files = {
        f"{IMAGES_DIR}/{name}/{key}": text
        for name, image in images.items()
        for key, text in image.data.items()
    }
    tags = {name: image.tag for name, image in images.items()}
    files[TAGS_FILE] = json.dumps(tags, indent=2, sort_keys=True) + "\n"
    return files


def ref_mismatches(
    directories: Mapping[str, Mapping[str, str]], tags: Mapping[str, str]
) -> list[str]:
    """Each image ref in `directories` that names a built image with a tag other than `tags`'.

    A ref is `/<name>:sha-<12 hex>`, which is how every consumer pin spells it. A ref to the
    mutable `latest` is not a content tag and is not compared.

    Args:
        directories: Each directory name under `cluster/` mapped to its files.
        tags: `tags.json`'s map.
    """
    if not tags:
        return []
    names = "|".join(re.escape(n) for n in sorted(tags, key=len, reverse=True))
    ref = re.compile(rf"/({names}):(sha-[0-9a-f]{{12}})\b")
    return [
        f"cluster/{directory}/{file} names {m.group(1)}:{m.group(2)}, and "
        f"{TAGS_FILE} names {tags[m.group(1)]}"
        for directory, files in sorted(directories.items())
        for file, text in sorted(files.items())
        for m in ref.finditer(text)
        if m.group(2) != tags[m.group(1)]
    ]


def _reaches_images(base: str, path: str) -> bool:
    """Whether `path`, relative to the bundle directory `base`, lands under `DIR/images/`."""
    joined = posixpath.normpath(posixpath.join(base, path))
    return joined == IMAGES_DIR or joined.startswith(IMAGES_DIR + "/")


def paths_into_images(files: Mapping[str, str]) -> list[str]:
    """Each Kustomization path in the bundle `files` that reaches `DIR/images/`.

    That is a Flux Kustomization's `spec.path` in `flux/*.yaml`, read from the bundle root, and
    a `resources` entry of a `cluster/<dir>/kustomization.yaml`, read from its directory.
    `../` is resolved first, so `./cluster/../images/nut` counts.
    """
    found = []
    for path, text in sorted(files.items()):
        parts = path.split("/")
        if parts[0] == FLUX_DIR and len(parts) == 2:
            for doc in yaml_fast.safe_load_all(text):
                spec_path = ((doc or {}).get("spec") or {}).get("path")
                if isinstance(spec_path, str) and _reaches_images("", spec_path):
                    found.append(f"{path}: spec.path {spec_path}")
        elif parts[0] == CLUSTER_DIR and parts[-1] == KUSTOMIZATION:
            base = posixpath.dirname(path)
            for doc in yaml_fast.safe_load_all(text):
                for entry in (doc or {}).get("resources") or []:
                    if isinstance(entry, str) and _reaches_images(base, entry):
                        found.append(f"{path}: resources {entry}")
    return found


def refuse_occupied(out: Path) -> None:
    """Refuse a render into an `out` whose `images/` already holds a file.

    Raises:
        RenderRefused: `out/images/` holds a file, which a stale render would leave beside
            the new contexts.
    """
    if (out / IMAGES_DIR).is_dir() and any((out / IMAGES_DIR).iterdir()):
        raise RenderRefused(
            f"{out / IMAGES_DIR} already holds files; render into an empty --out"
        )


def check_images(
    tags: list[RenderedTag],
    bundle: Mapping[str, str],
    images: Mapping[str, BuiltImage],
    out: Path,
) -> None:
    """Refuse a render whose image refs or Kustomization paths break C1's image rules.

    Raises:
        RenderRefused: `out/images/` already holds a file.
        RenderError: A rendered ref names a built image with a tag other than `tags.json`'s,
            or a Kustomization path reaches `DIR/images/`.
    """
    if images:
        refuse_occupied(out)
    directories = {name: f for t in tags for name, f in t.directories().items()}
    tag_map = {name: image.tag for name, image in images.items()}
    cluster_files = {
        f"{CLUSTER_DIR}/{name}/{file}": text
        for name, files in directories.items()
        for file, text in files.items()
    }
    problems = ref_mismatches(directories, tag_map) + paths_into_images(
        {**cluster_files, **bundle}
    )
    if problems:
        raise RenderError("\n".join(problems))
