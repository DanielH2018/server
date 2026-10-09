"""Render every k8s manifest template, for tests that need to assert on the OUTPUT.

Several guards can only be written against rendered manifests: a PVC's name and a container's
securityContext are both Jinja expressions in the template, so a text scan sees `{{ ... }}` and
silently matches nothing — which reads as "no findings" rather than "no coverage".

Rendering goes through validate.k8s_manifests' own machinery rather than a second stub set, so
what a test considers a manifest cannot drift from what that validator does.
"""

from lib import yaml_fast
from _role_census import role_dirs
from lib.render_context import render_context
from lib.repo_paths import HOST_VARS as HOST_VARS_DIR
from validate.k8s_manifests import (
    ALL_VARS,
    ANSIBLE,
    BASE_CONTEXT,
    HOST_VARS,
    K8S_ROLES,
    SHARED_TPL,
    SKIP_ROLES,
    claim_contexts,
    CLAIM_TEMPLATE,
    k8s_entries,
    load_yaml,
    make_env,
    make_lookup,
    register_ansible_filters,
    render_or_error,
    resolve_vars,
    role_defaults,
    shared_default_templates,
)


def _inventory_base(overrides: dict | None = None) -> dict:
    """The context every render here starts from, resolved once, `overrides` laid on top.

    daniel-box's host_vars layer over group_vars, as in the validator's main(): a template
    that reads `containers_list` itself (authelia's access_control rules) would otherwise
    iterate a StubUndefined as empty and hand every guard a Secret with the rules missing.
    """
    base = {
        **BASE_CONTEXT,
        **load_yaml(ALL_VARS),
        **load_yaml(HOST_VARS),
        "playbook_dir": str(ANSIBLE),
        **(overrides or {}),
    }
    return resolve_vars(base, base)


def _role_env(role_dir, ctx: dict):
    env = make_env([role_dir / "templates", SHARED_TPL])
    env.globals["lookup"] = make_lookup(ctx)
    register_ansible_filters(env)
    return env


def _render_texts(overrides: dict | None = None):
    """(role, template name, rendered TEXT) for every manifest template the validator renders.

    `overrides` go on top of each role's context, for a guard that sets a value the inventory
    does not hold. Raises on a render failure rather than skipping it — a template that
    stopped rendering would otherwise quietly drop out of every guard built on this.
    """
    # The context the validator renders with. `render_context` lays the overrides in before
    # anything resolves and on top again after: an inventory value or role default aliasing an
    # overridden secret (`x: "{{ some_secret }}"`) would otherwise resolve to the secret's STUB
    # before the override is ever laid on.
    entries = k8s_entries()

    for role_dir in role_dirs():
        role = role_dir.name
        if role in SKIP_ROLES or role not in entries:
            continue
        ctx = render_context(
            role_dir,
            overrides={"container_item": entries[role], **(overrides or {})},
            strict=True,
        )
        env = _role_env(role_dir, ctx)

        # The shared defaults come with the role's own templates: `k8s/manifests` renders a
        # manifest from `ansible/templates/` for a basename the role names and ships no
        # template for (`service.yaml` for 25 roles). Leaving them out would drop 25
        # Services out of every guard built on this, silently.
        own = sorted(role_dir.glob("templates/*.j2"))
        for tpl in own + shared_default_templates(role):
            if tpl.name.endswith(".sh.j2") or tpl.name.startswith("Dockerfile"):
                continue
            rendered, err = render_or_error(env, tpl.name, ctx)
            if rendered is None:
                raise AssertionError(f"{role}/{tpl.name} failed to render: {err}")
            yield role, tpl.name, rendered
        # `k8s_claims` renders one shared template per entry, so it is reached by the role's
        # declared data rather than by any template it ships.
        for claim_ctx in claim_contexts(ctx):
            rendered, err = render_or_error(env, CLAIM_TEMPLATE.name, claim_ctx)
            if rendered is None:
                raise AssertionError(
                    f"{role}/{CLAIM_TEMPLATE.name} failed to render: {err}"
                )
            yield role, CLAIM_TEMPLATE.name, rendered


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

    A full render costs ~0.95s, so each call site paying it separately would add tens of
    seconds to the suite. The render is a pure function of the
    repo tree, which no test writes to, so one result serves the whole session.

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


def render_texts(overrides: dict) -> tuple[tuple[str, str, str], ...]:
    """`rendered_texts()` with `overrides` laid over every role's context, uncached.

    For a census across the tree at a value the inventory does not hold — a secret set to a
    sentinel, so the census sees every role the secret's VALUE reaches, aliases included,
    where a source scan sees only the roles that spell its name.
    """
    return tuple(_render_texts(overrides))


def host_context(host: str = "daniel-box") -> dict:
    """The resolved inventory a render for `host` starts from: base context, all.yml, host_vars.

    The same layering `_render_all` builds for daniel-box, with the host a parameter so a
    staging guard can render against `daniel-stage.yml`. Not cached: a caller lays role
    defaults and overrides on top, and `resolve_vars` is cheap next to the render itself.
    """
    base = {
        **BASE_CONTEXT,
        **load_yaml(ALL_VARS),
        **load_yaml(HOST_VARS_DIR / f"{host}.yml"),
        "playbook_dir": str(ANSIBLE),
    }
    return resolve_vars(base, base)


def render_role_template(
    role: str, template: str, overrides: dict | None = None, *, host: str = "daniel-box"
) -> str:
    """One role's template rendered as text, with `overrides` laid over the role's context.

    `overrides` is a dict rather than `**kwargs` because a caller may need to override a
    variable named `host` — the staging variables guard hands every unsupplied name in.

    For a guard that flips one variable — a `manage_*` flag, `k8s_built_images` — and asserts
    on what changes. `rendered_docs()` is the whole tree at inventory values; this is one
    template at values the inventory does not hold, so it is not cached and each call renders.

    `role` need not be a `containers_list` entry: an included helper role such as
    `k8s/image-builder` renders here too, with the variables its caller hands over passed as
    `overrides`.

    Role defaults go under the inventory, which is Ansible's own precedence and the order
    `lib.render_context` gives `_render_all` and the validator. It matters here because
    daniel-stage overrides `traefik_k8s_manage_crowdsec` on purpose, so a staging render with
    defaults on top would render the value the deploy never uses.
    """
    base = host_context(host)
    # A default rather than a raise: `k8s/image-builder` is included by caller roles and is not
    # a `containers_list` entry at all, so the lookup finds nothing for it and the bare `next()`
    # raised `StopIteration` before the render ever started. The key is LEFT OUT in that case
    # rather than set to None, so a template reading `container_item.name` renders `STUB` the way
    # every other undefined does here instead of raising an AttributeError.
    entry = next((c for c in base["containers_list"] if c["name"] == role), None)
    ctx = {
        **role_defaults(role, base),
        **base,
        **({"container_item": entry} if entry is not None else {}),
        **(overrides or {}),
    }
    env = make_env([K8S_ROLES / role / "templates", SHARED_TPL])
    env.globals["lookup"] = make_lookup(ctx)
    register_ansible_filters(env)
    rendered, err = render_or_error(env, template, ctx)
    assert rendered is not None, (
        f"{role}/{template} failed to render for {host} with {overrides}: {err}"
    )
    return rendered


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
    # Overrides before resolution and on top again after, for the reason `_render_texts` gives.
    base = _inventory_base(overrides)
    entries = k8s_entries()
    for role_dir in role_dirs():
        role = role_dir.name
        if role in SKIP_ROLES or role not in entries:
            continue
        ctx = {
            **base,
            **role_defaults(role, base),
            "container_item": entries[role],
            **(overrides or {}),
        }
        env = _role_env(role_dir, ctx)
        for tpl in sorted(role_dir.glob(f"templates/{BUILD_TEMPLATE_GLOB}")):
            rendered, err = render_or_error(env, tpl.name, ctx)
            if rendered is None:
                raise AssertionError(f"{role}/{tpl.name} failed to render: {err}")
            yield role, tpl.name, rendered


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
}


def rendered_build_job_text() -> str:
    """`k8s/image-builder`'s build Job as a caller's include renders it, as TEXT."""
    return render_role_template(
        "image-builder", "build-job.yaml.j2", IMAGE_BUILDER_CALLER_VARS
    )


K8S_PLAY_NAME = "Deploy k8s workloads"


def deploy_play() -> dict:
    """deploy.yml's k8s play, selected by its exact name.

    deploy.yml opens with the Docker play for the Pi, so the k8s play is not the first. An
    exact name rather than a substring, so a second play with `k8s` in its name cannot be
    picked up in its place.
    """
    for play in yaml_fast.safe_load((ANSIBLE / "deploy.yml").read_text()) or []:
        if play.get("name") == K8S_PLAY_NAME:
            return play
    raise AssertionError(f"deploy.yml no longer has a play named {K8S_PLAY_NAME!r}")


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
