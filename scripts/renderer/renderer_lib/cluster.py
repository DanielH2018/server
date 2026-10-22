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

One rule is the renderer's alone: a role's `templates/verify/*.yaml.j2` (`verify_files`) render
unlabelled into `DIR/cluster/<tag>-verify/`, the verify and post-apply mutation Jobs Flux runs
once the tag is applied (D5.1a1). Its `templates/precheck/*.yaml.j2` (`precheck_files`) render
the same way into `DIR/cluster/<tag>-precheck/`, the check Jobs the tag waits on (#5719).
Ansible never reads either directory. Those templates also see `flux_job_image`, the image
every such Job runs, and `flux_job_connect`, the text of the helper a Job script that dials a
Service carries beside it.

A role's vendored files are no template, and `attach_vendored` adds them to `DIR/cluster/<tag>/`
byte for byte after every pass that edits rendered text (H1.1a). `rendered_docs()` in
`ansible/tests/_k8s_render.py` renders `templates/*.j2` alone, so the fleet guards never read
them; H1's *Vendored upstream manifests* names the guards that bypasses and why it is accepted.

A value a role task computes, such as a content tag or a script checksum, comes from
`task_inputs.derived_facts` and goes in as a `set_fact` would, over every file layer.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

import jinja2
from ansible.errors import AnsibleError

from deploy_tools.render_targets import render_targets
from lib import yaml_fast
from lib.ansible_inventory import K8S_HOST
from lib.ansible_jinja_env import ansible_bool
from lib.k8s_roles import (
    CLAIM_TEMPLATE,
    SHARED_MANIFEST_DEFAULTS,
    resolved_manifest_files,
    role_fields,
    tag_owners,
)
from lib.k8s_context import resolve_vars
from lib.render_guard import SHARED_TPL, StrictChainableUndefined
from lib.repo_paths import ANSIBLE, FILTER_PLUGINS, HOST_VARS, K8S_ROLES
from renderer.renderer_lib.bundle import (
    KUSTOMIZATION,
    PATCHES_DIR,
    SHARED_KINDS,
    kustomization_text,
    reserved_tags,
)
from renderer.renderer_lib.context import play_context
from renderer.renderer_lib.environment import (
    RenderError,
    loader_name,
    render_file,
    renderer_env,
)
from renderer.renderer_lib.ordering import (
    DEFERRED_SUFFIX,
    PRECHECK_SUFFIX,
    PRECHECK_TEMPLATES_DIR,
    PREPULL_SUFFIX,
    SNAPSHOT_SUFFIX,
    VERIFY_SUFFIX,
    VERIFY_TEMPLATES_DIR,
    Ordering,
    precheck_files,
    verify_files,
)
from renderer.renderer_lib.suspension import SYNC_SUSPEND_KEY, sync_suspend_refusal
from renderer.renderer_lib.task_inputs import derived_facts, include_vars

# Ansible loads a filter plugin by file path; a script reaches it through this insert.
if str(FILTER_PLUGINS) not in _sys.path:
    _sys.path.insert(0, str(FILTER_PLUGINS))

from toposort import K8S_APPLIES_LAST, build_k8s_dep_map, filter_by_platform

LABEL_WRAPPER = SHARED_TPL / "role-labelled.yaml.j2"
MANIFESTS_DEFAULTS = K8S_ROLES / "manifests" / "defaults" / "main.yml"
FLUX_DEFAULTS = K8S_ROLES / "flux" / "defaults" / "main.yml"
FLUX_JOB_CONNECT = K8S_ROLES / "manifests" / "files" / "flux_job_connect.py"
GROUP_VARS = ANSIBLE / "inventory" / "group_vars" / "all.yml"
# Each group variable naming a namespace that a role creates, mapped to that role. A deferred
# document in that namespace gives `<tag>-deferred` an edge to the role (D4.2).
NAMESPACE_ROLE_VARS = {"k8s_observability_namespace": "observability"}
# A role's vendored files sit in these subdirectories of `files/` and keep them under
# `cluster/<tag>/` (H1 *Vendored upstream manifests*): upstream manifests byte for byte, and
# Kustomize patches. H1.1d's `health_vendored.py` reads the same two.
VENDORED_DIRS = ("upstream", PATCHES_DIR)
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


@dataclass(frozen=True)
class RenderedTag:
    """One tag's rendered directories.

    Attributes:
        tag: The deploy tag, and the directory's name under `DIR/cluster/`.
        files: Each file's name mapped to its rendered text, `kustomization.yaml` included.
        deferred: The same for `DIR/cluster/<tag>-deferred/`, empty for a role that defers
            nothing.
        snapshot: The same for `DIR/cluster/<tag>-snapshot/`, which `snapshot.attach_snapshots`
            adds last, for a published tag with snapshot claims (D5.2a).
        prepull: The same for `DIR/cluster/<tag>-prepull/`, which `prepull.attach_prepulls`
            adds, for a published tag whose role sets `flux_prepull_timeout` (#5525).
        verify: The same for `DIR/cluster/<tag>-verify/`, empty for a role that ships no
            `templates/verify/` (D5.1a1).
        precheck: The same for `DIR/cluster/<tag>-precheck/`, empty for a role that ships no
            `templates/precheck/` (#5719).
    """

    tag: str
    files: dict[str, str]
    deferred: dict[str, str] = field(default_factory=dict)
    snapshot: dict[str, str] = field(default_factory=dict)
    prepull: dict[str, str] = field(default_factory=dict)
    verify: dict[str, str] = field(default_factory=dict)
    precheck: dict[str, str] = field(default_factory=dict)

    def directories(self) -> dict[str, dict[str, str]]:
        """Each directory name under `DIR/cluster/` mapped to the files it holds."""
        dirs = {self.tag: self.files}
        if self.deferred:
            dirs[self.tag + DEFERRED_SUFFIX] = self.deferred
        if self.snapshot:
            dirs[self.tag + SNAPSHOT_SUFFIX] = self.snapshot
        if self.prepull:
            dirs[self.tag + PREPULL_SUFFIX] = self.prepull
        if self.precheck:
            dirs[self.tag + PRECHECK_SUFFIX] = self.precheck
        if self.verify:
            dirs[self.tag + VERIFY_SUFFIX] = self.verify
        return dirs

    def with_directories(self, dirs: Mapping[str, dict[str, str]]) -> "RenderedTag":
        """This tag holding `dirs`, a map shaped as `directories` returns it.

        A pass that edits every directory's text rebuilds the tag through this, so no
        per-tag kind is dropped on the way.
        """
        return replace(
            self,
            files=dirs[self.tag],
            deferred=dirs.get(self.tag + DEFERRED_SUFFIX, {}),
            snapshot=dirs.get(self.tag + SNAPSHOT_SUFFIX, {}),
            prepull=dirs.get(self.tag + PREPULL_SUFFIX, {}),
            verify=dirs.get(self.tag + VERIFY_SUFFIX, {}),
            precheck=dirs.get(self.tag + PRECHECK_SUFFIX, {}),
        )


def select_tags(
    requested: list[str] | None,
    host: str = K8S_HOST,
    host_vars: Path = HOST_VARS,
    kinds: frozenset[str] = SHARED_KINDS,
) -> list[str]:
    """The sorted tags to render: `requested`, or every render target of `host`.

    A tag Flux applies is a render target too: `render_targets` keeps it, because the bundle
    publishes it.

    Raises:
        RenderRefused: A requested tag is not one of `host`'s render targets, or a tag to
            render takes a reserved name: the root Kustomization's, `homelab`, a member of
            `kinds` or of `RESERVED_NAMES`, or any name ending in `-deferred`, `-snapshot`,
            `-prepull`, `-precheck` or `-verify` (C3 *Reserved names*, D4.2, D5.1a1).
    """
    targets = set(render_targets(host, host_vars))
    tags = sorted(set(requested or targets))
    reserved = reserved_tags(tags, kinds)
    if reserved:
        raise RenderRefused(
            f"{reserved} take a name the bundle reserves for a Kustomization that is no "
            "deploy tag's: the root homelab, a SHARED_KINDS or RESERVED_NAMES member, or "
            "a name ending in -deferred, -snapshot, -prepull, -precheck or -verify, which a "
            "tag's own "
            "kinds take"
        )
    unknown = sorted(set(tags) - targets)
    if unknown:
        raise RenderRefused(f"{unknown} are not k8s render targets of {host}")
    return tags


def flux_owned_tags(host: str = K8S_HOST, host_vars: Path = HOST_VARS) -> set[str]:
    """The deploy tags of `host`'s k8s entries that carry `owner: flux` (C4).

    `tag_owners` maps them, the reader `promote.py published-tags` uses too, so the tags this
    refusal checks are the tags the bundle publishes.
    """
    path = host_vars / f"{host}.yml"
    if not path.exists():
        return set()
    return {tag for tag, owner in tag_owners(path).items() if owner == "flux"}


def bundle_ordering(
    owned: set[str],
    host: str = K8S_HOST,
    host_vars: Path = HOST_VARS,
    group_vars: Path = GROUP_VARS,
    roles: Path = K8S_ROLES,
) -> Ordering:
    """What the bundle generates `dependsOn` from: the play's own dependency map (D4.2).

    The map is `build_k8s_dep_map` over the full k8s `containers_list`, as `deploy.yml`
    builds it, never a `--role` subset, so a tag's edges do not depend on what else renders.
    The suspended tags are read over every flux-owned role for the same reason (D5.1b).

    Raises:
        ValueError: A flux-owned role's `flux_suspend`, `flux_prune` or
            `manifests_rollout_timeout` is not a literal (`role_fields`).
    """
    fields, problems = role_fields(owned, roles)
    if problems:
        raise ValueError("; ".join(problems))
    entries = yaml_fast.safe_load((host_vars / f"{host}.yml").read_text())
    k8s = filter_by_platform(entries.get("containers_list") or [], "k8s")
    group = yaml_fast.safe_load(group_vars.read_text())
    return Ordering(
        deps=build_k8s_dep_map(k8s, str(ANSIBLE)),
        applies_last=frozenset(K8S_APPLIES_LAST),
        namespace_roles={
            str(group[var]): role for var, role in NAMESPACE_ROLE_VARS.items()
        },
        flux_owned=frozenset(owned),
        suspended=frozenset(tag for tag, own in fields.items() if own.get("suspend")),
    )


def rollout_timeout_default() -> str:
    """`manifests_rollout_timeout_default` as the manifests role's own defaults set it."""
    return str(
        yaml_fast.safe_load(MANIFESTS_DEFAULTS.read_text())[
            "manifests_rollout_timeout_default"
        ]
    )


def _manifests_prune_default() -> object:
    """`manifests_prune` as the manifests role's own defaults set it."""
    return yaml_fast.safe_load(MANIFESTS_DEFAULTS.read_text())["manifests_prune"]


def _sources(
    tag: str, names: set[str], own_only: bool, templates: Path | None = None
) -> dict[str, Path]:
    """The template each manifest basename renders from, as the manifests role resolves it.

    The role's own `<templates>/<name>.j2` wins, `templates` defaulting to the tree role's
    `templates/`. A basename in `SHARED_MANIFEST_DEFAULTS` the role ships no template for takes
    the shared default, unless `own_only`, as for secret files.

    Raises:
        RenderError: A basename has no template, which Ansible reports as "Could not find or
            access" the template.
    """
    templates = templates or K8S_ROLES / tag / "templates"
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


def tag_context(
    role_dir: Path, tag: str, overrides: dict, host: str = K8S_HOST
) -> dict:
    """`play_context` with `container_item`, the k8s play's loop variable, set to `tag`'s entry."""
    ctx = play_context(role_dir, overrides, host)
    ctx["container_item"] = next(e for e in ctx["containers_list"] if e["name"] == tag)
    return ctx


def render_tag(
    tag: str,
    stubs: dict,
    host: str = K8S_HOST,
    built: dict[str, str] | None = None,
    roles: Path = K8S_ROLES,
) -> RenderedTag:
    """Render every file the manifests role writes for `tag`, with a `kustomization.yaml` each.

    The role's `templates/verify/` and `templates/precheck/` render too, into
    `RenderedTag.verify` and `RenderedTag.precheck`.

    Args:
        tag: The deploy tag.
        stubs: The extra vars that outrank every other layer: a mode's secret values.
        host: The inventory host whose context the tag renders in.
        built: The images `tag`'s role builds, each mapped to its content tag, as the
            caller computed them for `DIR/images/tags.json`. None computes them here.
        roles: The role tree `tag`'s role is read from; a test passes a fixture copy.

    Raises:
        RenderError: A template fails to render, a strict undefined among the causes, or a
            listed basename has no template.
    """
    role_dir = roles / tag
    try:
        ctx = tag_context(role_dir, tag, stubs, host)
        # A role default can read a fact a task sets, as every `*_k8s_image` built here reads
        # `k8s_built_image_tags`. The first pass leaves such a default undefined, so the facts
        # go in as the overrides of a second pass, as `set_fact` outranks every file layer.
        # The stub file's extra vars outrank them in turn.
        facts = derived_facts(role_dir, ctx, built)
        ctx = tag_context(role_dir, tag, {**facts, **stubs}, host)
    except (ValueError, jinja2.TemplateError, AnsibleError) as exc:
        raise RenderError(f"{tag}: {exc}") from exc
    entry = ctx["container_item"]
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
    refusal = sync_suspend_refusal(tag, ansible_bool(ctx.get(SYNC_SUSPEND_KEY, False)))
    if refusal:
        raise RenderError(refusal)
    files, secret = resolved_manifest_files(tag, roles, entry=entry)
    if "manifests_files" in include:
        files = set(include["manifests_files"])
    if "manifests_secret_files" in include:
        secret = set(include["manifests_secret_files"])
    deferred = set(include.get("manifests_deferred_files") or [])
    # Through `bool`, as the render tasks read it: an include's quoted "false" is False.
    prune = ansible_bool(include.get("manifests_prune", _manifests_prune_default()))

    env = renderer_env(role_dir / "templates", ctx)

    def render(path: Path, out_name: str, wrap: bool, **loop_vars) -> str:
        vars_ = {**ctx, **loop_vars}
        if wrap:
            vars_["manifests_label_src"] = str(path)
            path = LABEL_WRAPPER
        try:
            return render_file(env, loader_name(env, path), vars_)
        except (jinja2.TemplateError, ValueError, AnsibleError) as exc:
            raise RenderError(f"{tag}/{out_name} from {path.name}: {exc}") from exc

    rendered = {}
    templates = role_dir / "templates"
    for name, src in {
        **_sources(tag, files, False, templates),
        **_sources(tag, secret, True, templates),
    }.items():
        rendered[name] = render(src, name, prune, item=name)
    for claim in ctx.get("k8s_claims") or []:
        name = f"claim-{claim['name']}.yaml"
        rendered[name] = render(CLAIM_TEMPLATE, name, prune, manifests_claim=claim)
    rendered[KUSTOMIZATION] = kustomization_text(list(rendered))

    later = {}
    for name, src in _sources(tag, deferred, True, templates).items():
        later[name] = render(src, name, False, item=name)
    if later:
        later[KUSTOMIZATION] = kustomization_text(list(later))

    # DECIDED: `-verify` renders whatever owns the tag, as `-deferred` does. Only a published
    # tag gets the Kustomization that applies it, and the shadow diff reports the directory
    # of an Ansible-owned tag as C1-only, so a role adds `templates/verify/` in the PR that
    # hands its tag to Flux (D5's *How a role declares a verify Job*). `-precheck` follows
    # the same rule.
    verifying = set(verify_files([tag], roles)[tag])
    checking = set(precheck_files([tag], roles)[tag])
    # Every Job Flux applies runs `flux_job_image`, a flux role default no other role's
    # context holds. Passing it here keeps a verify template from `lookup()`ing the flux
    # defaults, which the deployer's cross-role scan would read as the role depending on them.
    # The helper's text comes the same way, so a helper fix does not redeploy every verifying role.
    image = _flux_job_image() if verifying or checking else None
    helper = FLUX_JOB_CONNECT.read_text() if verifying or checking else None
    jobs = {}
    for kind, names in (
        (VERIFY_TEMPLATES_DIR, verifying),
        (PRECHECK_TEMPLATES_DIR, checking),
    ):
        jobs[kind] = {
            name: render(
                src,
                name,
                False,
                item=name,
                flux_job_image=image,
                flux_job_connect=helper,
            )
            for name, src in _sources(tag, names, True, templates / kind).items()
        }
        if jobs[kind]:
            jobs[kind][KUSTOMIZATION] = kustomization_text(list(jobs[kind]))
    return RenderedTag(
        tag,
        rendered,
        later,
        verify=jobs[VERIFY_TEMPLATES_DIR],
        precheck=jobs[PRECHECK_TEMPLATES_DIR],
    )


def _flux_job_image() -> str:
    """The `flux_job_image` pin, read from the tree's flux role defaults."""
    return str(yaml_fast.safe_load(FLUX_DEFAULTS.read_text())["flux_job_image"])


def vendored_files(tag: str, roles: Path = K8S_ROLES) -> dict[str, str]:
    """`tag`'s vendored files by their path under `DIR/cluster/<tag>/`, each text verbatim.

    A role keeps them under `files/upstream/` and `files/patches/`, and each keeps its
    subdirectory, so no vendored name can collide with a rendered one. The text is decoded
    from the file's bytes, with no newline translation.
    """
    return {
        f"{sub}/{path.name}": path.read_bytes().decode()
        for sub in VENDORED_DIRS
        for path in sorted((roles / tag / "files" / sub).glob("*.y*ml"))
        if path.is_file()
    }


def attach_vendored(
    rendered: list[RenderedTag],
    key_sets: Mapping[str, frozenset[str]] | None = None,
    roles: Path = K8S_ROLES,
) -> list[RenderedTag]:
    """`rendered` with each tag's vendored files added to its own directory (H1.1a).

    The tag's `kustomization.yaml` is rebuilt to list the upstream files under `resources`
    and the patches under `patches`. `<tag>-deferred` gets none.

    DECIDED: attached last, after the restart stamp, the prune label and the SOPS copies,
    because each of those edits rendered text or rebuilds the kustomization. Upstream bytes
    stay as upstream wrote them, so the sha256 pin still describes what Flux applies. A
    Namespace in an upstream file therefore carries no prune label; the plumbing tags that
    vendor set `flux_prune: false`, which leaves every object of the tag in place anyway.

    Args:
        rendered: The rendered tags.
        key_sets: A placeholder render's key set per directory. When passed, a tag whose
            own directory holds a placeholder and whose role vendors files is refused, so
            that directory never substitutes and no `${` in upstream bytes reaches Flux's
            substitution (C1 *Vendored files*). A stub render passes none.
        roles: The role tree the vendored files are read from.

    Raises:
        RenderError: A tag that vendors files holds a placeholder.
    """
    attached = []
    for tag in rendered:
        vendored = vendored_files(tag.tag, roles)
        if not vendored:
            attached.append(tag)
            continue
        keys = sorted((key_sets or {}).get(tag.tag) or ())
        if keys:
            raise RenderError(
                f"{tag.tag} vendors {sorted(vendored)} and its render holds the "
                f"placeholders {keys}. A directory with vendored files must not substitute "
                "(C1 *Vendored files*): move the templates that read a secret to a tag of "
                "their own"
            )
        files = {k: v for k, v in tag.files.items() if k != KUSTOMIZATION}
        files |= vendored
        files[KUSTOMIZATION] = kustomization_text(list(files))
        attached.append(replace(tag, files=files))
    return attached


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
            # A vendored file sits one directory down, under `upstream/` or `patches/`.
            (dest / file).parent.mkdir(parents=True, exist_ok=True)
            (dest / file).write_text(text)
            written.append(dest / file)
    return written
