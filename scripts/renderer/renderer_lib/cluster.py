"""Render k8s roles into C1's cluster layout: `DIR/cluster/<tag>/` and its `kustomization.yaml`.

Each tag renders its role's own manifest templates with the play-time context from
`renderer_lib.context`, through the repo's Jinja environment and a strict undefined. A name
no layer supplies stops the render, where the render guards' `StubUndefined` would print
`STUB` into the output.

Target slice R1 widens what renders one PR at a time, and `RENDERABLE` names the tags that
render so far. A tag outside it is refused rather than rendered without the inputs a later
PR adds. The shared `service.yaml` and `ingressroute.yaml` defaults, the claim files, the
deferred directory and the `role-labelled.yaml.j2` wrapper arrive in R1's PR 2, so until
then a render is not yet byte-identical to the Ansible manifests role's.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from dataclasses import dataclass
from pathlib import Path

import jinja2
import yaml

from deploy_tools.render_targets import render_targets
from lib.ansible_inventory import K8S_HOST
from lib.ansible_jinja_env import template_env
from lib.k8s_roles import resolved_manifest_files
from lib.k8s_yaml import make_lookup
from lib.render_guard import StrictChainableUndefined
from lib.repo_paths import K8S_ROLES
from renderer.renderer_lib.context import play_context

# The tags that render with every input their templates read. R1's PR 2 and PR 3 add the
# file rules and the task-computed inputs, and each widens this set.
RENDERABLE = frozenset({"littlelink"})

KUSTOMIZATION = "kustomization.yaml"


class RenderRefused(ValueError):
    """A requested render that the renderer will not start; nothing has been written."""


class RenderError(RuntimeError):
    """A template that failed to render; nothing has been written."""


@dataclass(frozen=True)
class RenderedTag:
    """One tag's rendered directory.

    Attributes:
        tag: The deploy tag, and the directory's name under `DIR/cluster/`.
        files: Each file's name mapped to its rendered text, `kustomization.yaml` included.
    """

    tag: str
    files: dict[str, str]


def select_tags(requested: list[str] | None, host: str = K8S_HOST) -> list[str]:
    """The sorted tags to render: `requested`, or every renderable target of `host`.

    Raises:
        RenderRefused: A requested tag is not one of `host`'s render targets, or does not
            render yet.
    """
    targets = set(render_targets(host))
    if not requested:
        return sorted(RENDERABLE & targets)
    unknown = sorted(set(requested) - targets)
    if unknown:
        raise RenderRefused(f"{unknown} are not k8s render targets of {host}")
    pending = sorted(set(requested) - RENDERABLE)
    if pending:
        raise RenderRefused(
            f"{pending} do not render yet: target slice R1 adds the shared defaults, claims "
            f"and task-computed inputs they need in later PRs. Renderable: {sorted(RENDERABLE)}"
        )
    return sorted(set(requested))


def kustomization_text(files: list[str]) -> str:
    """A `kustomization.yaml` listing `files` in lexical order.

    Lexical order is the order `kubectl apply -f <dir>/` reads a directory in.
    """
    return yaml.safe_dump(
        {
            "apiVersion": "kustomize.config.k8s.io/v1beta1",
            "kind": "Kustomization",
            "resources": sorted(files),
        },
        sort_keys=False,
    )


def render_tag(tag: str, stubs: dict, host: str = K8S_HOST) -> RenderedTag:
    """Render `tag`'s own manifest templates and the `kustomization.yaml` that lists them.

    Raises:
        RenderError: A template fails to render, a strict undefined among the causes.
    """
    role_dir = K8S_ROLES / tag
    ctx = play_context(role_dir, stubs, host)
    # The k8s play's loop variable over its view of containers_list.
    ctx["container_item"] = next(e for e in ctx["containers_list"] if e["name"] == tag)
    files, secret = resolved_manifest_files(tag, entry=ctx["container_item"])
    templates = role_dir / "templates"
    names = sorted(n for n in files | secret if (templates / f"{n}.j2").is_file())
    env = template_env(templates, undefined_cls=StrictChainableUndefined)
    env.globals["lookup"] = make_lookup(ctx)
    rendered = {}
    for name in names:
        try:
            rendered[name] = env.get_template(f"{name}.j2").render(ctx)
        except jinja2.TemplateError as exc:
            raise RenderError(f"{tag}/templates/{name}.j2: {exc}") from exc
    rendered[KUSTOMIZATION] = kustomization_text(names)
    return RenderedTag(tag, rendered)


def write_cluster(rendered: list[RenderedTag], out: Path) -> list[Path]:
    """Write each tag's directory under `out/cluster/`, and return the files written.

    Raises:
        RenderRefused: A tag's directory already holds a file, which a stale render would
            leave behind beside the new one. Checked for every tag before any write.
    """
    cluster = out / "cluster"
    occupied = sorted(
        r.tag
        for r in rendered
        if (cluster / r.tag).is_dir() and any((cluster / r.tag).iterdir())
    )
    if occupied:
        raise RenderRefused(
            f"{cluster} already holds {occupied}; render into an empty --out"
        )
    written = []
    for tag in rendered:
        dest = cluster / tag.tag
        dest.mkdir(parents=True, exist_ok=True)
        for name, text in sorted(tag.files.items()):
            (dest / name).write_text(text)
            written.append(dest / name)
    return written
