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

No Kustomization applies these files. D5.4a2's `images` job builds and pushes them to GHCR.
`ghcr_refs` points each flux-owned tag's refs to a built image at that push, with
`imagePullPolicy: IfNotPresent` beside each (D5.4b). An Ansible-owned tag keeps the in-cluster
registry and its `Always`. `pull_policy_offences` refuses a GHCR content-tag ref whose policy is
`Always` or unset. The plan is docs/target-architecture/deploy/d5-bulk-move.md, *Images built
by CI (D5.4)*.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import json
import posixpath
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

import jinja2
import yaml
from ansible.errors import AnsibleError

from lib import yaml_fast
from lib.ansible_inventory import K8S_HOST
from lib.repo_paths import K8S_ROLES
from renderer.renderer_lib.bundle import CLUSTER_DIR, FLUX_DIR, KUSTOMIZATION
from renderer.renderer_lib.cluster import RenderedTag, RenderRefused, tag_context
from renderer.renderer_lib.environment import RenderError
from renderer.renderer_lib.ordering import DEFERRED_SUFFIX, SNAPSHOT_SUFFIX
from renderer.renderer_lib.task_inputs import image_builder_sites, image_contexts

IMAGES_DIR = "images"
TAGS_FILE = f"{IMAGES_DIR}/tags.json"
# A GHCR content tag is immutable under `ci_images`' push-once rule, so a node's cached copy is
# always the right image. Under `Always`, kubelet asks GHCR on every container start, so a GHCR
# or WAN outage would fail every restart, reboot and crash recovery of the consumer.
GHCR_PULL_POLICY = "IfNotPresent"
# A built-image ref as a role default spells it: `<pull host>/<name>:<content tag>`. A GHCR ref
# has a second `/` before the name, so it never matches.
BUILT_REF = re.compile(
    r"[^/\s]+/(?P<name>[a-z0-9]+(?:-[a-z0-9]+)*):(?P<tag>sha-[0-9a-f]{12})"
)
# Where `ci_images` pushes each image. A host render imports this module, and `ci_images` is no
# `HOST_INPUTS` member, so the prefix is repeated here and a test holds the two equal.
GHCR = "ghcr.io/danielh2018"
GHCR_REF = re.compile(re.escape(GHCR) + r"/[^:\s]+:sha-[0-9a-f]{12}")


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


def _containers(node: yaml.Node | None) -> Iterator[tuple[yaml.MappingNode, dict]]:
    """Each mapping under `node` with an `image` key, and its keys to their key and value nodes."""
    if isinstance(node, yaml.SequenceNode):
        for item in node.value:
            yield from _containers(item)
    elif isinstance(node, yaml.MappingNode):
        keys = {
            k.value: (k, v) for k, v in node.value if isinstance(k, yaml.ScalarNode)
        }
        if "image" in keys:
            yield node, keys
        for _, value in node.value:
            yield from _containers(value)


def ghcr_text(text: str, tags: Mapping[str, str], where: str) -> str:
    """`text` with each built-image ref at GHCR and `imagePullPolicy: IfNotPresent` beside it.

    A built-image ref is an `image` that names one of `tags` with its tag, on any host. The
    policy's value is replaced, or, for a container that sets none, a line is inserted after
    the `image` line. Every other byte is kept, as `restart.stamp` keeps them.

    Raises:
        RenderError: A container that sets no policy is a flow mapping, which a line insert
            cannot extend.
    """
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    edits = []
    for doc in yaml.compose_all(text, Loader=yaml.SafeLoader):
        for container, keys in _containers(doc):
            key, image = keys["image"]
            m = BUILT_REF.fullmatch(str(image.value))
            if not m or tags.get(m["name"]) != m["tag"]:
                continue
            new = f"{GHCR}/{m['name']}:{m['tag']}"
            edits.append((image.start_mark.index, image.end_mark.index, new))
            if "imagePullPolicy" in keys:
                policy = keys["imagePullPolicy"][1]
                edits.append(
                    (policy.start_mark.index, policy.end_mark.index, GHCR_PULL_POLICY)
                )
            elif container.flow_style:
                raise RenderError(f"{where}: {new} is in a flow mapping with no policy")
            else:
                at = offsets[min(image.end_mark.line + 1, len(lines))]
                indent = " " * key.start_mark.column
                edits.append((at, at, f"{indent}imagePullPolicy: {GHCR_PULL_POLICY}\n"))
    for start, end, new in sorted(edits, reverse=True):
        text = text[:start] + new + text[end:]
    return text


def ghcr_refs(
    rendered: list[RenderedTag], owned: Iterable[str], images: Mapping[str, BuiltImage]
) -> list[RenderedTag]:
    """`rendered` with each flux-owned tag's built-image refs pointed at GHCR (D5.4b).

    DECIDED: a pass over the rendered text, not a role variable. A tag moves to GHCR, and to
    `IfNotPresent`, in the render of the PR that marks it `owner: flux`, and no role's
    defaults or templates change, so Ansible keeps the in-cluster registry and `Always` for
    every tag it still owns. Only a ref to a `tags.json` image moves, so any other image of a
    flux-owned tag keeps its host, as registry's self-test probe does.

    Args:
        rendered: The tags to edit.
        owned: The flux-owned tags.
        images: Every built image, as `tags.json` names them.
    """
    owned = set(owned)
    tags = {name: image.tag for name, image in images.items()}
    pointed = []
    for tag in rendered:
        if tag.tag not in owned or not tags:
            pointed.append(tag)
            continue
        dirs = {
            name: {f: ghcr_text(t, tags, f"{name}/{f}") for f, t in files.items()}
            for name, files in tag.directories().items()
        }
        pointed.append(
            replace(
                tag,
                files=dirs[tag.tag],
                deferred=dirs.get(tag.tag + DEFERRED_SUFFIX, {}),
                snapshot=dirs.get(tag.tag + SNAPSHOT_SUFFIX, {}),
            )
        )
    return pointed


def pull_policy_offences(directories: Mapping[str, Mapping[str, str]]) -> list[str]:
    """Each GHCR content-tag ref in `directories` whose `imagePullPolicy` is `Always` or unset.

    Args:
        directories: Each directory name under `cluster/` mapped to its files.
    """
    found = []
    for directory, files in sorted(directories.items()):
        for file, text in sorted(files.items()):
            if not GHCR_REF.search(text):
                continue
            for doc in yaml.compose_all(text, Loader=yaml.SafeLoader):
                for _, keys in _containers(doc):
                    image = str(keys["image"][1].value)
                    policy = keys.get("imagePullPolicy")
                    value = str(policy[1].value) if policy else "unset"
                    if GHCR_REF.fullmatch(image) and value in ("unset", "Always"):
                        found.append(
                            f"cluster/{directory}/{file} pulls {image} with "
                            f"imagePullPolicy {value}, so every container start asks "
                            f"GHCR; a content tag takes {GHCR_PULL_POLICY}"
                        )
    return found


def _reaches_images(base: str, path: str) -> bool:
    """Whether `path`, relative to the bundle directory `base`, lands under `DIR/images/`.

    A leading `/` names the bundle root, as Flux joins `spec.path` onto the artifact root.
    """
    joined = (
        posixpath.normpath(path.lstrip("/"))
        if path.startswith("/")
        else posixpath.normpath(posixpath.join(base, path))
    )
    return joined == IMAGES_DIR or joined.startswith(IMAGES_DIR + "/")


def _kustomize_paths(doc: dict) -> list[tuple[str, str]]:
    """Each path a kustomize `kustomization.yaml` document reads, with the field naming it."""
    found = [
        (field, entry)
        for field in ("resources", "components")
        for entry in doc.get(field) or []
        if isinstance(entry, str)
    ]
    found += [
        ("patches", patch["path"])
        for patch in doc.get("patches") or []
        if isinstance(patch, dict) and isinstance(patch.get("path"), str)
    ]
    return found


def paths_into_images(files: Mapping[str, str]) -> list[str]:
    """Each Kustomization path in the bundle `files` that reaches `DIR/images/`.

    That is a Flux Kustomization's `spec.path` in `flux/*.yaml`, read from the bundle root,
    and every `resources`, `components` and `patches[].path` entry of any
    `kustomization.yaml`, the flux index included, read from its directory. `../` and a
    leading `/` are resolved first, so `./cluster/../images/nut` and `/images/nut` count.
    """
    found = []
    for path, text in sorted(files.items()):
        parts = path.split("/")
        base = posixpath.dirname(path)
        for doc in yaml_fast.safe_load_all(text):
            if not isinstance(doc, dict):
                continue
            spec_path = (doc.get("spec") or {}).get("path")
            if (
                parts[0] == FLUX_DIR
                and isinstance(spec_path, str)
                and _reaches_images("", spec_path)
            ):
                found.append(f"{path}: spec.path {spec_path}")
            if parts[-1] == KUSTOMIZATION:
                found += [
                    f"{path}: {field} {entry}"
                    for field, entry in _kustomize_paths(doc)
                    if _reaches_images(base, entry)
                ]
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
            a GHCR content-tag ref pulls `Always` or sets no policy, or a Kustomization path
            reaches `DIR/images/`.
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
    problems = (
        ref_mismatches(directories, tag_map)
        + pull_policy_offences(directories)
        + paths_into_images({**cluster_files, **bundle})
    )
    if problems:
        raise RenderError("\n".join(problems))
