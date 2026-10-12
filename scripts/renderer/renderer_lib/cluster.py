"""Render k8s roles into C1's cluster layout: `DIR/cluster/<tag>/` and its `kustomization.yaml`.

Each tag renders what the Ansible manifests role renders for it, with the play-time context
from `renderer_lib.context`, through the repo's Jinja environment and a strict undefined. A
name no layer supplies stops the render, where the render guards' `StubUndefined` would print
`STUB` into the output.

The manifests role's file rules, as "Render manifests", "Render secret manifests", "Render
volume claims" and "Render deferred manifests" in `ansible/roles/k8s/manifests/tasks/main.yml`
apply them:

- The file lists come from `resolved_manifest_files`, or from the include's own
  `manifests_files` and `manifests_secret_files` where it passes them.
- A basename the role ships no template for renders from the shared default in
  `ansible/templates/`, `service.yaml` and `ingressroute.yaml`.
- Each `k8s_claims` entry renders `claim-default.yaml.j2` to `claim-<name>.yaml`.
- A prune-armed role renders every one of those through `role-labelled.yaml.j2`, which stamps
  `homelab/role` on each document.
- `manifests_deferred_files` render from the role's own templates, unlabelled, into
  `DIR/cluster/<tag>-deferred/`.

`PENDING` names the tags that read a value a role task computes. R1's PR 3 derives those
values, and until then a pending tag is refused rather than rendered without them.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import jinja2
import yaml
from ansible.errors import AnsibleError
from jinja2 import pass_context
from jinja2.runtime import Context

from deploy_tools.render_targets import render_targets
from lib import yaml_fast
from lib.ansible_inventory import K8S_HOST
from lib.ansible_jinja_env import ansible_bool, template_env
from lib.k8s_roles import (
    CLAIM_TEMPLATE,
    SHARED_MANIFEST_DEFAULTS,
    resolved_manifest_files,
)
from lib.k8s_context import resolve_vars
from lib.k8s_yaml import make_lookup
from lib.render_guard import SHARED_TPL, StrictChainableUndefined
from lib.repo_paths import K8S_ROLES
from renderer.renderer_lib.context import play_context
from renderer.renderer_lib.task_inputs import include_vars

# The tags whose templates or defaults read a value a role task computes, a name in
# `task_inputs.TASK_COMPUTED`. R1's PR 3 derives those values and empties this set. A test
# holds it equal to a word grep of each tag's role for those names.
PENDING = frozenset(
    {
        "autofix-bridge",
        "code-server",
        "game-stats",
        "homelab-mcp",
        "ical-proxy",
        "janitorr",
        "karakeep",
        "monitor-bridge",
        "n8n",
        "nut",
        "pi-peer-backup",
        "terraria",
        "uptime-kuma",
        "valheim",
    }
)

KUSTOMIZATION = "kustomization.yaml"
DEFERRED_SUFFIX = "-deferred"
LABEL_WRAPPER = SHARED_TPL / "role-labelled.yaml.j2"
MANIFESTS_DEFAULTS = K8S_ROLES / "manifests" / "defaults" / "main.yml"
# The include vars that decide what the manifests role renders and where.
INCLUDE_KEYS = frozenset(
    {
        "manifests_files",
        "manifests_secret_files",
        "manifests_deferred_files",
        "manifests_deferred_dir_name",
        "manifests_prune",
    }
)


class RenderRefused(ValueError):
    """A requested render that the renderer will not start; nothing has been written."""


class RenderError(RuntimeError):
    """A template that failed to render; nothing has been written."""


@dataclass(frozen=True)
class RenderedTag:
    """One tag's rendered directories.

    Attributes:
        tag: The deploy tag, and the directory's name under `DIR/cluster/`.
        files: Each file's name mapped to its rendered text, `kustomization.yaml` included.
        deferred: The same for `DIR/cluster/<tag>-deferred/`, empty for a role that defers
            nothing.
    """

    tag: str
    files: dict[str, str]
    deferred: dict[str, str] = field(default_factory=dict)

    def directories(self) -> dict[str, dict[str, str]]:
        """Each directory name under `DIR/cluster/` mapped to the files it holds."""
        dirs = {self.tag: self.files}
        if self.deferred:
            dirs[self.tag + DEFERRED_SUFFIX] = self.deferred
        return dirs


def select_tags(requested: list[str] | None, host: str = K8S_HOST) -> list[str]:
    """The sorted tags to render: `requested`, or every renderable target of `host`.

    Raises:
        RenderRefused: A requested tag is not one of `host`'s render targets, or does not
            render yet.
    """
    targets = set(render_targets(host))
    if not requested:
        return sorted(targets - PENDING)
    unknown = sorted(set(requested) - targets)
    if unknown:
        raise RenderRefused(f"{unknown} are not k8s render targets of {host}")
    pending = sorted(set(requested) & PENDING)
    if pending:
        raise RenderRefused(
            f"{pending} do not render yet: they read values a role task computes, which "
            "target slice R1 derives in a later PR"
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


def renderer_env(templates: Path) -> jinja2.Environment:
    """The strict environment a role's templates render in: its own directory, then the shared one.

    The template census in `scripts/renderer/tests/` parses every template through this
    environment, so a filter or test the renderer lacks fails there before a render meets it.
    """
    return template_env(templates, undefined_cls=StrictChainableUndefined)


def _template_lookup(env: jinja2.Environment, ctx: dict):
    """`make_lookup`, with `template` rendered through `env` and the calling template's variables.

    The labelling wrapper calls `lookup('ansible.builtin.template', manifests_label_src)`, and
    the inner template reads the task's loop variable (`manifests_claim`). So the lookup renders
    with the caller's variables, through the same strict environment and loader, where
    `make_lookup` renders with the role context and a `StubUndefined` environment.
    """
    base = make_lookup(ctx)

    @pass_context
    def lookup(context: Context, kind: str, *args, **kwargs) -> object:
        if kind.removeprefix("ansible.builtin.") != "template":
            return base(context, kind.removeprefix("ansible.builtin."), *args, **kwargs)
        name = _loader_name(env, Path(args[0]))
        # Rstripped as `make_lookup` does, so a template the guards and the renderer both
        # embed reads the same bytes in each.
        return env.get_template(name).render(context.get_all()).rstrip("\n")

    return lookup


def _loader_name(env: jinja2.Environment, path: Path) -> str:
    """`path` as a name `env`'s loader resolves, relative to the first search path holding it.

    Raises:
        RenderError: `path` lies under none of the loader's search paths.
    """
    for root in cast(jinja2.FileSystemLoader, env.loader).searchpath:
        if path.is_relative_to(root):
            return path.relative_to(root).as_posix()
    raise RenderError(f"{path} is outside the role's templates and ansible/templates/")


def _manifests_prune_default() -> object:
    """`manifests_prune` as the manifests role's own defaults set it."""
    return yaml_fast.safe_load(MANIFESTS_DEFAULTS.read_text())["manifests_prune"]


def _sources(tag: str, names: set[str], own_only: bool) -> dict[str, Path]:
    """The template each manifest basename renders from, as the manifests role resolves it.

    The role's own `templates/<name>.j2` wins. A basename in `SHARED_MANIFEST_DEFAULTS` the role
    ships no template for takes the shared default, unless `own_only`, as for secret files.

    Raises:
        RenderError: A basename has no template, which Ansible reports as "Could not find or
            access" the template.
    """
    templates = K8S_ROLES / tag / "templates"
    sources = {}
    for name in sorted(names):
        own = templates / f"{name}.j2"
        if own.is_file():
            sources[name] = own
        elif not own_only and name in SHARED_MANIFEST_DEFAULTS:
            sources[name] = SHARED_TPL / SHARED_MANIFEST_DEFAULTS[name]
        else:
            raise RenderError(
                f"{tag}: {name} is in its file list and has no template {own}"
            )
    return sources


def render_tag(tag: str, stubs: dict, host: str = K8S_HOST) -> RenderedTag:
    """Render every file the manifests role writes for `tag`, with a `kustomization.yaml` each.

    Raises:
        RenderError: A template fails to render, a strict undefined among the causes, or a
            listed basename has no template.
    """
    role_dir = K8S_ROLES / tag
    try:
        ctx = play_context(role_dir, stubs, host)
    except ValueError as exc:
        raise RenderError(f"{tag}: {exc}") from exc
    # The k8s play's loop variable over its view of containers_list.
    entry = next(e for e in ctx["containers_list"] if e["name"] == tag)
    ctx["container_item"] = entry
    try:
        raw = include_vars(role_dir)
        # Only the keys the render reads. The rest (rollout timeouts, extra rollouts) may name
        # facts a role task sets, which Ansible resolves only where a task reads them.
        read = {k: v for k, v in raw.items() if k in INCLUDE_KEYS}
        include = resolve_vars(
            read, {**ctx, **read}, undefined_cls=StrictChainableUndefined
        )
    except (ValueError, jinja2.TemplateError) as exc:
        raise RenderError(f"{tag}: its k8s/manifests include: {exc}") from exc
    # The include's vars outrank the role defaults and the inventory; the stub file's extra
    # vars outrank them in turn.
    ctx: dict = {**ctx, **include, **stubs, "manifests_service": tag}
    files, secret = resolved_manifest_files(tag, entry=entry)
    if "manifests_files" in include:
        files = set(include["manifests_files"])
    if "manifests_secret_files" in include:
        secret = set(include["manifests_secret_files"])
    deferred = set(include.get("manifests_deferred_files") or [])
    # Through `bool`, as the render tasks read it: an include's quoted "false" is False.
    prune = ansible_bool(include.get("manifests_prune", _manifests_prune_default()))

    env = renderer_env(role_dir / "templates")
    env.globals["lookup"] = _template_lookup(env, ctx)

    def render(path: Path, out_name: str, wrap: bool, **loop_vars) -> str:
        vars_ = {**ctx, **loop_vars}
        if wrap:
            vars_["manifests_label_src"] = str(path)
            path = LABEL_WRAPPER
        try:
            return env.get_template(_loader_name(env, path)).render(vars_)
        except (jinja2.TemplateError, ValueError, AnsibleError) as exc:
            raise RenderError(f"{tag}/{out_name} from {path.name}: {exc}") from exc

    rendered = {}
    for name, src in {
        **_sources(tag, files, False),
        **_sources(tag, secret, True),
    }.items():
        rendered[name] = render(src, name, prune, item=name)
    for claim in ctx.get("k8s_claims") or []:
        name = f"claim-{claim['name']}.yaml"
        rendered[name] = render(CLAIM_TEMPLATE, name, prune, manifests_claim=claim)
    rendered[KUSTOMIZATION] = kustomization_text(list(rendered))

    later = {}
    for name, src in _sources(tag, deferred, True).items():
        later[name] = render(src, name, False, item=name)
    if later:
        later[KUSTOMIZATION] = kustomization_text(list(later))
    return RenderedTag(tag, rendered, later)


def write_cluster(rendered: list[RenderedTag], out: Path) -> list[Path]:
    """Write each tag's directories under `out/cluster/`, and return the files written.

    Raises:
        RenderRefused: A directory to write already holds a file, which a stale render would
            leave behind beside the new one. Checked for every directory before any write.
    """
    cluster = out / "cluster"
    dirs = {name: files for r in rendered for name, files in r.directories().items()}
    occupied = sorted(
        name
        for name in dirs
        if (cluster / name).is_dir() and any((cluster / name).iterdir())
    )
    if occupied:
        raise RenderRefused(
            f"{cluster} already holds {occupied}; render into an empty --out"
        )
    written = []
    for name, files in sorted(dirs.items()):
        dest = cluster / name
        dest.mkdir(parents=True, exist_ok=True)
        for file, text in sorted(files.items()):
            (dest / file).write_text(text)
            written.append(dest / file)
    return written
