"""Render every k8s manifest template, for tests that need to assert on the OUTPUT.

Several guards can only be written against rendered manifests: a PVC's name and a container's
securityContext are both Jinja expressions in the template, so a text scan sees `{{ ... }}` and
silently matches nothing — which reads as "no findings" rather than "no coverage".

Each manifest text is `render_tag`'s, stub mode, with its strict undefined and its file rules.
So a guard reads what the renderer writes for a tag, the `homelab/role` label included, before
the passes that stamp restarts and swap in placeholders.
"""

import contextlib
import functools

import jinja2

from lib import yaml_fast
from _role_census import role_dirs
from lib.render_context import render_context
from renderer.renderer_lib.bundle import KUSTOMIZATION
from renderer.renderer_lib.cluster import play_context, render_tag, role_context
from renderer.renderer_lib.environment import RenderError, render_file, renderer_env
from renderer.shadow_diff import stub_vars
from validate.k8s_manifests import K8S_ROLES, SKIP_ROLES, k8s_entries


def _stubs(overrides: dict | None) -> dict:
    """Every secret's stub with `overrides` on top: the extra vars a stub render takes."""
    return {**stub_vars(), **(overrides or {})}


def _render_texts(overrides: dict | None = None, *, only: frozenset[str] | None = None):
    """(role, template name, rendered TEXT) for every manifest `render_tag` writes for a role.

    `overrides` go on top of each role's context, for a guard that sets a value the inventory
    does not hold, and `only` keeps the named roles. A shared default or a claim carries its
    source template's name. A template out of the role's file lists is not here, and a guard
    reads one with `render_role_template`. Raises `RenderError` on a render failure rather
    than skipping it — a template that stopped rendering would otherwise quietly drop out of
    every guard built on this.
    """
    entries = k8s_entries()

    for role_dir in role_dirs():
        role = role_dir.name
        if role in SKIP_ROLES or role not in entries:
            continue
        if only is not None and role not in only:
            continue
        tag = render_tag(role, _stubs(overrides))
        for files in (tag.files, tag.deferred):
            for name, text in sorted(files.items()):
                if name != KUSTOMIZATION:
                    yield role, tag.sources[name], text


def _render_all():
    """(role, template name, parsed doc) for every manifest the validator would render."""
    for role, name, rendered in _render_texts():
        _TEXTS.append((role, name, rendered))
        for doc in yaml_fast.safe_load_all(rendered):
            if isinstance(doc, dict) and doc.get("kind"):
                yield role, name, doc


_CACHE: tuple | None = None
# Filled by _render_all as a side effect, because the render is the expensive part and a
# second pass over the tree would double it. Only reachable through rendered_texts(), which
# forces the render first.
_TEXTS: list[tuple[str, str, str]] = []


def rendered_docs():
    """The rendered manifests, rendering the tree at most once per process.

    A full render costs ~8s, so each call site paying it separately would add tens of
    seconds to the suite. The render is a pure function of the
    repo tree, which no test writes to, so one result serves the whole session.
    `ansible/tests/conftest.py` builds it through `prebuild()` when collection finishes, so
    the cost lands outside every test's timed phases.

    # DECIDED: shared docs, not deep copies. Every call site iterates and asserts; none mutates
    # a doc, and copying 323 dicts per call would spend a slice of what the cache saves. A test
    # that needs to mutate one must copy it itself — mutating in place corrupts every later
    # test in the same worker.
    """
    global _CACHE
    if _CACHE is None:
        _TEXTS.clear()
        _CACHE = tuple(_render_all())
    return iter(_CACHE)


def rendered_texts():
    """(role, template name, rendered TEXT) for every manifest template.

    The docs above are what a manifest means; this is what it looks like. A reader that parses
    YAML by position — manifest_declares.py, which runs stdlib-only on the host — has to be
    tested against the bytes, because re-serialising a parsed doc normalises exactly the
    formatting such a reader could trip over.
    """
    rendered_docs()
    return iter(tuple(_TEXTS))


def prebuild(read=rendered_docs) -> None:
    """Build the `rendered_docs()` cache now, and leave a failed render for a test to report.

    A render that raises leaves `_CACHE` empty, so the first test to read the render renders
    again and fails naming the template. Raising here instead would end a collection hook's
    caller in an INTERNALERROR that names no test.

    Args:
        read: what builds the cache. A test seam; the hook passes nothing.
    """
    with contextlib.suppress(Exception):
        read()


def render_texts(
    overrides: dict, *, only: frozenset[str] | None = None
) -> tuple[tuple[str, str, str], ...]:
    """`rendered_texts()` with `overrides` laid over every role's context, uncached.

    For a census across the tree at a value the inventory does not hold — a secret set to a
    sentinel, so the census sees every role the secret's VALUE reaches, aliases included,
    where a source scan sees only the roles that spell its name.
    """
    return tuple(_render_texts(overrides, only=only))


def host_context(host: str = "daniel-box") -> dict:
    """The resolved inventory a render for `host` starts from: base context, all.yml, host_vars.

    `render_context` with no role's defaults: the plane directory holds no defaults file. The
    host is a parameter so a staging guard can render against `daniel-stage.yml`. Not cached:
    resolving the inventory is cheap next to the render itself.
    """
    return render_context(K8S_ROLES, host=host, strict=True)


def render_role_template(
    role: str, template: str, overrides: dict | None = None, *, host: str = "daniel-box"
) -> str:
    """One role's template rendered as text, with `overrides` laid over the role's context.

    `overrides` is a dict rather than `**kwargs` because a caller may need to override a
    variable named `host` — the staging variables guard hands every unsupplied name in.

    For a guard that flips one variable, such as a `manage_*` flag, and asserts
    on what changes. `rendered_docs()` is the whole tree at inventory values; this is one
    template at values the inventory does not hold, so it is not cached and each call renders.

    `role` need not be a `containers_list` entry: an included helper role such as
    `k8s/image-builder` renders here too, with the variables its caller hands over passed as
    `overrides`.

    The context is `role_context`'s, the one `render_tag` renders with. Role defaults go under
    the inventory, which is Ansible's own precedence. It matters here because daniel-stage
    overrides `traefik_k8s_manage_crowdsec` on purpose, so a staging render with defaults on
    top would render the value the deploy never uses. `overrides` go in before anything
    resolves, so a default derived from an overridden flag (traefik's `k8s_claims` from
    `traefik_k8s_manage_acme`) follows the flag. A name no layer supplies fails the render, so
    a guard passes every variable a caller role hands over.
    """
    ctx = (
        _inventory_context(role, host)
        if not overrides
        else _template_context(role, overrides, host)
    )
    env = renderer_env(K8S_ROLES / role / "templates", ctx)
    try:
        return render_file(env, template, ctx)
    except jinja2.TemplateError as exc:
        raise AssertionError(
            f"{role}/{template} failed to render for {host} with {overrides}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


@functools.cache
def _entry_names(host: str) -> frozenset[str]:
    return frozenset(c["name"] for c in host_context(host)["containers_list"])


@functools.cache
def _inventory_context(role: str, host: str) -> dict:
    """`_template_context` at inventory values, once per process: a context costs about 100ms."""
    return _template_context(role, None, host)


def _template_context(role: str, overrides: dict | None, host: str) -> dict:
    """The context one role's template renders with, for a role in no `containers_list` too."""
    stubs = _stubs(overrides)
    if role in _entry_names(host):
        return role_context(role, stubs, host)[0]
    return {**play_context(K8S_ROLES / role, stubs, host), "manifests_service": role}


def render_pod_env(
    role: str,
    overrides: dict | None = None,
    *,
    secret: str = "env-secret.yaml.j2",
    deployment: str = "deployment.yaml.j2",
) -> dict[str, str]:
    """The environment a role's first container receives, rendered: env var -> value.

    The Deployment's `env` values over the env Secret's `stringData`, the precedence Kubernetes
    gives `env` over `envFrom`. Since #5574 the bridges keep only credentials in the Secret and
    every setting in `env`, so a guard reading either alone sees half the pod's environment.
    """
    data = yaml_fast.safe_load(render_role_template(role, secret, overrides))
    assert data.get("kind") == "Secret", f"{role}/{secret} renders no Secret"
    workload = yaml_fast.safe_load(render_role_template(role, deployment, overrides))
    container = workload["spec"]["template"]["spec"]["containers"][0]
    plain = {e["name"]: e["value"] for e in container.get("env", []) if "value" in e}
    return {**data["stringData"], **plain}


# `_render_all` skips `Dockerfile*` because its output feeds a YAML parse. A build pin is still
# a rendered value — code-server's Dockerfile names its node URL and every extension URL
# through a role default — so the build files get their own accessor and their own cache (#3175).
def traefik_static_config(
    overrides: dict | None = None, *, host: str = "daniel-box"
) -> dict:
    """Traefik's static config itself, parsed, not the ConfigMap that wraps it.

    `static-config.yaml.j2`'s data value is a block scalar, so the config is a STRING at the
    manifest level and has to be parsed a second time. Checked on the render rather than the
    template's text: the indirection trap in `textual-guard-checks-break-on-indirection`.
    """
    doc = yaml_fast.safe_load(
        render_role_template("traefik", "static-config.yaml.j2", overrides, host=host)
    )
    return yaml_fast.safe_load(doc["data"]["traefik.yml"])


BUILD_TEMPLATE_GLOB = "Dockerfile*.j2"

# The roles whose build the accessor must reach, so a renamed or moved template fails as a
# missing member rather than as a guard over an empty set. k8s/image-builder renders each of
# these through `lookup('template', image_builder_dockerfile)` from the CALLER's role context,
# which is the context `_render_build_files` lays out.
BUILD_ROLES = frozenset(
    {
        "code-server",
        "homelab-mcp",
        "ical-proxy",
        "karakeep",
        "n8n",
        "nut",
        "pi-peer-backup",
        "terraria",
        "valheim",
    }
)

_BUILD_TEXTS: tuple[tuple[str, str, str], ...] | None = None


def _render_build_files(overrides: dict | None = None):
    entries = k8s_entries()
    for role_dir in role_dirs():
        role = role_dir.name
        if role in SKIP_ROLES or role not in entries:
            continue
        templates = sorted(role_dir.glob(f"templates/{BUILD_TEMPLATE_GLOB}"))
        if not templates:
            continue
        ctx = role_context(role, _stubs(overrides))[0]
        env = renderer_env(role_dir / "templates", ctx)
        for tpl in templates:
            try:
                yield role, tpl.name, render_file(env, tpl.name, ctx)
            except jinja2.TemplateError as exc:
                raise RenderError(f"{role}/{tpl.name}: {exc}") from exc


def rendered_build_texts() -> tuple[tuple[str, str, str], ...]:
    """(role, template name, rendered TEXT) for every k8s role's `Dockerfile*.j2`.

    What the BUILD reads, not what produces it: a pin a role default supplies arrives expanded
    here, where a source scan sees `{{ ... }}` and matches nothing. Raises on a render failure
    for the reason `_render_all` does — a template that stopped rendering would otherwise drop
    out of every guard built on this.

    Cached for the process, like `rendered_docs`.
    """
    global _BUILD_TEXTS
    if _BUILD_TEXTS is None:
        _BUILD_TEXTS = tuple(_render_build_files())
    return _BUILD_TEXTS


def render_build_texts(overrides: dict) -> tuple[tuple[str, str, str], ...]:
    """`rendered_build_texts()` with `overrides` laid over every role's context, uncached.

    The build-file counterpart of `render_texts`, for a census at a sentinel value (#3191).
    """
    return tuple(_render_build_files(overrides))


def rendered_build_text(role: str, template: str = "Dockerfile.j2") -> str:
    """One entry of `rendered_build_texts`, by role and template name.

    Fails naming the template when the set does not hold it, so a guard over a renamed
    Dockerfile reads as a failure rather than as a pass over nothing.
    """
    for name, tpl, text in rendered_build_texts():
        if (name, tpl) == (role, template):
            return text
    raise AssertionError(
        f"{role}/{template} is not among the rendered build files: "
        f"{sorted((n, t) for n, t, _ in rendered_build_texts())}"
    )


def rendered_k8s_text(role: str, template: str) -> str:
    """One entry of `rendered_texts`, by role and template name.

    The k8s counterpart of `_setup_render.rendered_setup_text` and
    `_shell_render.rendered_shell_text`. Fails naming the template when the render set does not
    hold it — a role outside `k8s_entries()`, a template in a nested `templates/` subdirectory
    (which `_render_texts` does not glob), or a renamed file. Each of those would otherwise
    hand a guard a render of nothing.
    """
    texts = [text for r, t, text in rendered_texts() if (r, t) == (role, template)]
    if not texts:
        raise AssertionError(
            f"k8s/{role}/{template} is not among the rendered manifests: "
            f"{sorted((r, t) for r, t, _ in rendered_texts())}"
        )
    return "\n".join(texts)


# `k8s/image-builder` is a CALLER_RENDERED_ROLES member, so `rendered_docs()` never reaches its
# build Job: the image, context and tag arrive on the calling role's `include_role` task. These
# stand in for that task, so a guard over every pod the cluster runs can still parse this one.
IMAGE_BUILDER_CALLER_VARS = {
    "image_builder_name": "example-image",
    "image_builder_context_dir": "/tmp/example-context",
    "image_builder_tag": "abc1234",
    "image_builder_dockerfile": "Dockerfile",
    "image_builder_content_tag": "sha-abc1234",
}


def rendered_build_job_text() -> str:
    """`k8s/image-builder`'s build Job as a caller's include renders it, as TEXT."""
    return render_role_template(
        "image-builder", "build-job.yaml.j2", IMAGE_BUILDER_CALLER_VARS
    )


def pod_template(doc: dict) -> dict:
    """The pod template a rendered workload stamps its pods from, or {} if it has none.

    A Deployment's pod template is `spec.template`. A CronJob's is one level deeper, at
    `spec.jobTemplate.spec.template`; reading a CronJob the Deployment way silently returns
    {}, so a guard over it passes having checked nothing. Explicit nulls read as absent.
    """
    spec = doc.get("spec") or {}
    if doc.get("kind") == "CronJob":
        spec = (spec.get("jobTemplate") or {}).get("spec") or {}
    return spec.get("template") or {}


def pod_spec(doc: dict) -> dict:
    """The pod spec of a rendered workload, CronJobs included, or {} if it has none."""
    return pod_template(doc).get("spec") or {}
