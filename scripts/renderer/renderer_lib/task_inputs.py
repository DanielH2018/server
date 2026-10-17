"""The values Ansible computes in role tasks that the renderer must supply itself.

`include_vars` reads the `vars:` a role passes to its `k8s/manifests` include.

`derived_facts` computes the values a role task derives from the tree, `TASK_COMPUTED`, by
evaluating the task's own expression read out of its tasks file: each image's content tag by
image-builder's expression, the script checksums, `kuma_declarations` and `janitorr_dry_run`.
`playbook_dir` and `role_path` resolve as Ansible resolves them, so a lookup reads the same
file a deploy reads.

No value a template reads comes from live state, so the renderer pins no fact. Target slice
R3 removed the last live reads, and R3's live-read census in `scripts/renderer/tests/` holds
the tree to that.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from dataclasses import dataclass
from pathlib import Path
from typing import cast

import jinja2

from lib import yaml_fast
from lib.repo_paths import ANSIBLE, K8S_ROLES
from renderer.renderer_lib.environment import evaluate, renderer_env


IMAGE_BUILDER = K8S_ROLES / "image-builder"
CONTENT_TAG = "image_builder_content_tag"
BUILT_IMAGE_TAGS = "k8s_built_image_tags"
# The `data` the content tag's expression hashes, read the way that expression reads it.
_CONTEXT_DATA = (
    "{{ (lookup('template', 'context-configmap.yaml.j2') | from_yaml)['data'] }}"
)

# Each role's `set_fact` values that `derived_facts` evaluates, in the order the role's tasks
# set them, as the tasks file and the fact.
SET_FACTS = {
    "autofix-bridge": (("main.yml", "autofix_bridge_script_checksum"),),
    "janitorr": (("main.yml", "janitorr_dry_run"),),
    "monitor-bridge": (("main.yml", "monitor_bridge_script_checksum"),),
    "uptime-kuma": (("main.yml", "kuma_declarations"),),
}

# The values a role task computes that a template or a role default reads. A test holds this
# set equal to a word grep of the k8s templates and defaults for every `set_fact` name.
TASK_COMPUTED = frozenset(
    {
        BUILT_IMAGE_TAGS,
        "monitor_bridge_script_checksum",
        "autofix_bridge_script_checksum",
        "kuma_declarations",
        "janitorr_dry_run",
    }
)

# The modules that include a role, by every name ansible-core accepts.
_INCLUDE_MODULES = (
    "include_role",
    "import_role",
    "ansible.builtin.include_role",
    "ansible.builtin.import_role",
)


def _tasks(nodes: list) -> list[dict]:
    """Every task in a tasks file, the ones inside `block`, `rescue` and `always` included."""
    found = []
    for task in nodes or []:
        if not isinstance(task, dict):
            continue
        found.append(task)
        for key in ("block", "rescue", "always"):
            found.extend(_tasks(task.get(key)))
    return found


def include_vars(role_dir: Path) -> dict:
    """The `manifests_*` vars `role_dir`'s tasks pass to the `k8s/manifests` include that renders.

    That is the include with no `tasks_from`, or `tasks_from: main`. Another entry point, such
    as configarr's `drain.yml` or pihole's `rollout_amend.yml`, renders nothing. The values are
    read as written, so a Jinja value is still a string for the caller to resolve.

    Raises:
        ValueError: The role's tasks hold no such include, or more than one.
    """
    found = []
    for path in sorted((role_dir / "tasks").glob("*.yml")):
        for task in _tasks(yaml_fast.safe_load(path.read_text())):
            for module in _INCLUDE_MODULES:
                args = task.get(module)
                if not isinstance(args, dict) or args.get("name") != "k8s/manifests":
                    continue
                if Path(args.get("tasks_from") or "main").stem != "main":
                    continue
                found.append(
                    {
                        k: v
                        for k, v in (task.get("vars") or {}).items()
                        if k.startswith("manifests_")
                    }
                )
    if len(found) != 1:
        raise ValueError(
            f"{role_dir.name} includes k8s/manifests to render {len(found)} times, not once"
        )
    return found[0]


@dataclass(frozen=True)
class BuildSite:
    """One `include_role` of `k8s/image-builder`, as its role's tasks file writes it.

    Attributes:
        vars: The include's `vars:`, unresolved, so a value or a key may still hold Jinja.
        when: The include's `when:` conditions, empty for an unconditional include.
    """

    vars: dict
    when: tuple[str, ...] = ()


def image_builder_sites(role_dir: Path) -> list[BuildSite]:
    """Every `k8s/image-builder` include in `role_dir`'s tasks files, in file order."""
    sites = []
    for path in sorted((role_dir / "tasks").glob("*.yml")):
        for task in _tasks(yaml_fast.safe_load(path.read_text())):
            for module in _INCLUDE_MODULES:
                args = task.get(module)
                if isinstance(args, dict) and args.get("name") == "k8s/image-builder":
                    when = task.get("when") or ()
                    sites.append(
                        BuildSite(
                            dict(task.get("vars") or {}),
                            (when,) if isinstance(when, str) else tuple(when),
                        )
                    )
    return sites


def set_fact_task(path: Path, fact: str) -> tuple[str, dict]:
    """The value the one `set_fact` task in `path` that sets `fact` assigns, with its `vars:`.

    The value is the task's text verbatim, so the renderer evaluates the role's own
    expression rather than a restatement of it.

    Raises:
        ValueError: No task in `path` sets `fact`, or more than one does.
    """
    found = [
        (task["ansible.builtin.set_fact"][fact], dict(task.get("vars") or {}))
        for task in _tasks(yaml_fast.safe_load(path.read_text()))
        if fact in (task.get("ansible.builtin.set_fact") or {})
    ]
    if len(found) != 1:
        raise ValueError(f"{len(found)} set_fact tasks in {path} set {fact}, not one")
    return found[0]


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


def _template_vars(env: jinja2.Environment, task_vars: dict, variables: dict) -> dict:
    """`_template` over a task's `vars:` mapping, which stays a mapping."""
    return cast(dict, _template(env, task_vars, variables))


def site_variables(site: BuildSite, ctx: dict) -> dict:
    """The variables image-builder's tasks see for `site` in a role whose context is `ctx`.

    image-builder's own defaults sit under the calling role's variables, and the include's
    `vars:` over both, with `playbook_dir` resolving to `ansible/`.
    """
    defaults = yaml_fast.safe_load(
        (IMAGE_BUILDER / "defaults" / "main.yml").read_text()
    )
    variables = {**defaults, **ctx, "playbook_dir": str(ANSIBLE)}
    env = renderer_env(IMAGE_BUILDER / "templates", variables)
    return {**variables, **_template_vars(env, site.vars, variables)}


def content_tag(variables: dict) -> str:
    """`image_builder_content_tag` for one build, by image-builder's own expression.

    Args:
        variables: The build's variables, as `site_variables` returns them.
    """
    expression, _ = set_fact_task(IMAGE_BUILDER / "tasks" / "main.yml", CONTENT_TAG)
    env = renderer_env(IMAGE_BUILDER / "templates", variables)
    return str(evaluate(env, expression, variables))


@dataclass(frozen=True)
class ImageContext:
    """One build's rendered context and its content tag.

    Attributes:
        data: The `data` of image-builder's context ConfigMap, each file name mapped to its
            text, parsed as the content tag's expression parses it. It holds every byte the
            build reads.
        tag: `image_builder_content_tag`, by `content_tag`.
    """

    data: dict[str, str]
    tag: str


def image_contexts(role_dir: Path, ctx: dict) -> dict[str, ImageContext]:
    """Each image `role_dir` builds, mapped to its context and content tag.

    A build whose `when:` is false is left out, as Ansible skips it. The context renders
    `context-configmap.yaml.j2` through the lookup and `from_yaml` the content tag's expression applies,
    so a file written from `data` hashes to `tag`.
    """
    built = {}
    for site in image_builder_sites(role_dir):
        variables = site_variables(site, ctx)
        env = renderer_env(IMAGE_BUILDER / "templates", variables)
        if not all(env.compile_expression(cond)(**variables) for cond in site.when):
            continue
        data = evaluate(env, _CONTEXT_DATA, variables)
        built[variables["image_builder_name"]] = ImageContext(
            {str(k): str(v) for k, v in cast(dict, data).items()},
            content_tag(variables),
        )
    return built


def built_image_tags(role_dir: Path, ctx: dict) -> dict[str, str]:
    """`k8s_built_image_tags` after `role_dir`'s builds: each image's name mapped to its tag.

    `ansible/deploy.yml` resets the map to empty before the k8s play, and each build adds its
    own image, so a role that builds nothing reads an empty map. A build whose `when:` is false
    adds nothing, and its consumer falls back to `latest`.
    """
    return {name: c.tag for name, c in image_contexts(role_dir, ctx).items()}


def derived_facts(
    role_dir: Path, ctx: dict, built: dict[str, str] | None = None
) -> dict:
    """The values `role_dir`'s tasks compute that its templates and defaults read.

    That is `k8s_built_image_tags` for every role, and the `SET_FACTS` values of the roles
    that set one, each by its own task's expression with `playbook_dir` and `role_path` as
    Ansible sets them.

    Args:
        role_dir: The role directory, `ansible/roles/k8s/<tag>`.
        ctx: The role's play-time context, the stub file's variables included.
        built: `k8s_built_image_tags` as the caller already computed it from `ctx`, so a
            render's `DIR/images/tags.json` and its consumers' refs share one computation.
            None computes it here.

    Raises:
        ValueError: A `SET_FACTS` entry names a fact its tasks file does not set once.
        jinja2.TemplateError: An expression will not evaluate.
    """
    if built is None:
        built = built_image_tags(role_dir, ctx)
    facts: dict = {BUILT_IMAGE_TAGS: dict(built)}
    for tasks_file, fact in SET_FACTS.get(role_dir.name, ()):
        expression, task_vars = set_fact_task(role_dir / "tasks" / tasks_file, fact)
        variables = {
            **ctx,
            **facts,
            "playbook_dir": str(ANSIBLE),
            "role_path": str(role_dir),
        }
        env = renderer_env(role_dir / "templates", variables)
        variables.update(_template_vars(env, task_vars, variables))
        facts[fact] = evaluate(env, expression, variables)
    return facts
