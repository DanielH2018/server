"""R3's live-read census: no name a k8s-play task sets reaches a template the render reads.

Target slice R3's exit check 3 (`docs/target-architecture/render/r3-remove-live-reads.md`,
*The census*). The census computes three sets from the tree:

- **Set by tasks:** every `set_fact` key and `register` name in every task file the k8s play
  in `ansible/deploy.yml` reaches. The walk follows `import_tasks`, `include_tasks`,
  `import_role` and `include_role`, nested blocks included, and expands the batch loop's
  `k8s/{{ container_item.name }}` to every render target.
- **Derived:** the names the renderer computes from the tree. The caller supplies them, as
  `derived_names` computes them for the real tree.
- **Reached:** the names read by the templates the render reads, then, until nothing is
  added, the names each definition of a reached name reads. A definition is a role's
  `defaults/` or `vars/` value, an inventory value, an `include_role` `vars:` value or a
  `set_fact` value. A derived name stops the walk.

`_live_read_jinja.py` reads each template and definition: its names, and its lookups, of
which it accepts four plugins in fixed shapes. Each name a `vars` lookup source yields joins
the walk.

A name that is set by tasks, reached and not derived is an offender, and so is every lookup
the census cannot name the reads of. The test module is
`test_renderer_live_read_census.py`.
"""

import functools
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from _live_read_jinja import Reader, references, template_lookups

from deploy_tools.render_targets import render_targets
from lib import yaml_fast
from lib.ansible_inventory import K8S_HOST
from lib.k8s_roles import (
    CLAIM_TEMPLATE,
    SHARED_MANIFEST_DEFAULTS,
    k8s_entries,
    resolved_manifest_files,
)
from lib.render_guard import SHARED_TPL
from lib.repo_paths import ANSIBLE, K8S_ROLES
from renderer import shadow_diff
from renderer.renderer_lib.cluster import LABEL_WRAPPER, tag_context
from renderer.renderer_lib.task_inputs import derived_facts, include_vars
from toposort import filter_by_platform

K8S_PLAY = "Deploy k8s workloads"
ROLE_LOOP = "k8s/{{ container_item.name }}"
IMAGE_BUILDER = "k8s/image-builder"
# The image-builder template the build context renders from. Its sibling `build-job.yaml.j2`
# is a one-off Job image-builder's own task applies, so the render never reads it.
CONTEXT_TEMPLATE = "context-configmap.yaml.j2"
# The image-builder include vars that name a template the build context renders.
CONTEXT_VARS = ("image_builder_dockerfile", "image_builder_context")

SET_FACT = frozenset({"set_fact", "ansible.builtin.set_fact"})
INCLUDE_TASKS = frozenset(
    {
        "include_tasks",
        "import_tasks",
        "ansible.builtin.include_tasks",
        "ansible.builtin.import_tasks",
    }
)
INCLUDE_ROLE = frozenset(
    {
        "include_role",
        "import_role",
        "ansible.builtin.include_role",
        "ansible.builtin.import_role",
    }
)


class CensusError(ValueError):
    """A task include the walk cannot resolve to a file, so the census would miss its tasks."""


@dataclass(frozen=True)
class Tree:
    """The Ansible tree the census reads: the repo's, or a test fixture's.

    Attributes:
        ansible: The playbook directory, which holds `deploy.yml`, `roles/` and `inventory/`.
        host: The inventory host the k8s play runs on.
        shared: The shared templates directory a role's templates import from.
        always: Templates every tag's render reads, whatever its file list.
    """

    ansible: Path = ANSIBLE
    host: str = K8S_HOST
    shared: Path = SHARED_TPL
    always: tuple[Path, ...] = ()

    @property
    def roles(self) -> Path:
        return self.ansible / "roles"


@dataclass
class Census:
    """What one census run found.

    Attributes:
        set_by_tasks: Each name a task sets, mapped to every task file that sets it.
        reached: Each reached name, mapped to every template or definition that reads it.
        templates: Every template the render reads.
        accepted: Each accepted `pipe` or `vars` lookup, as `<plugin> in <file>`.
        vars_names: Every name a `vars` lookup source yields.
        problems: Each lookup the census cannot name the reads of.
        derived: The names the renderer computes, which stop the walk.
    """

    set_by_tasks: dict[str, set[str]] = field(default_factory=dict)
    reached: dict[str, set[str]] = field(default_factory=dict)
    templates: set[Path] = field(default_factory=set)
    accepted: list[str] = field(default_factory=list)
    vars_names: set[str] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)
    derived: frozenset[str] = frozenset()

    def offenders(self) -> list[str]:
        """Each task-set name the walk reaches that is not derived, then each lookup problem."""
        live = sorted((self.set_by_tasks.keys() & self.reached.keys()) - self.derived)
        return [
            f"{name}: set in {min(self.set_by_tasks[name])}, read by {_first_reader(self.reached[name])}"
            for name in live
        ] + self.problems


def _first_reader(readers: set[str]) -> str:
    """A template among `readers` if one reads the name, else the first definition."""
    return min(readers, key=lambda reader: (" in " in reader, reader))


@dataclass
class _Walk:
    files: set[Path] = field(default_factory=set)
    set_by_tasks: dict[str, set[str]] = field(default_factory=dict)
    # Each `set_fact` value, as the task file that sets it and the value.
    set_values: dict[str, list[tuple[str, object]]] = field(default_factory=dict)
    roles: set[Path] = field(default_factory=set)
    # Each included role's directory, mapped to the `vars:` of every include of it, with the
    # role directory of the task that includes it.
    include_vars: dict[Path, list[tuple[Path | None, dict]]] = field(
        default_factory=dict
    )


def _tasks(nodes_: list | None) -> Iterator[dict]:
    for task in nodes_ or []:
        if isinstance(task, dict):
            yield task
            for key in ("block", "rescue", "always"):
                yield from _tasks(task.get(key))


def _rel(tree: Tree, path: Path) -> str:
    return (
        str(path.relative_to(tree.ansible))
        if path.is_relative_to(tree.ansible)
        else str(path)
    )


def _role_of(tree: Tree, path: Path) -> Path | None:
    """The role directory `path` sits in, `roles/<plane>/<name>`, or None for a play file."""
    if not path.is_relative_to(tree.roles):
        return None
    parts = path.relative_to(tree.roles).parts
    return tree.roles / parts[0] / parts[1] if len(parts) > 2 else None


def _include_file(tree: Tree, current: Path, target: str) -> Path:
    role = _role_of(tree, current)
    target = target.replace("{{ playbook_dir }}", str(tree.ansible))
    if role is not None:
        target = target.replace("{{ role_path }}", str(role))
    if "{{" in target or "{%" in target:
        raise CensusError(f"{_rel(tree, current)}: cannot resolve include {target!r}")
    bases = [current.parent, *([role / "tasks"] if role else []), tree.ansible]
    for base in bases:
        candidate = (base / target).resolve()
        if candidate.is_file():
            return candidate
    raise CensusError(f"{_rel(tree, current)}: include {target!r} names no file")


def _role_tasks(tree: Tree, current: Path, args: dict, tags: list[str]) -> list[Path]:
    name = str(args.get("name", ""))
    names = [f"k8s/{tag}" for tag in tags] if name == ROLE_LOOP else [name]
    if any("{{" in n for n in names):
        raise CensusError(f"{_rel(tree, current)}: cannot resolve role {name!r}")
    tasks_from = str(args.get("tasks_from") or "main")
    if not Path(tasks_from).suffix:
        tasks_from += ".yml"
    found = []
    for role in names:
        path = tree.roles / role / "tasks" / tasks_from
        if not path.is_file():
            raise CensusError(f"{_rel(tree, current)}: role {role} has no {path.name}")
        found.append(path)
    return found


def _walk_file(tree: Tree, path: Path, tags: list[str], walk: _Walk) -> None:
    if path in walk.files:
        return
    walk.files.add(path)
    role = _role_of(tree, path)
    if role is not None:
        walk.roles.add(role)
    _walk_tasks(tree, path, yaml_fast.safe_load(path.read_text()), tags, walk)


def _walk_tasks(
    tree: Tree, path: Path, tasks: list, tags: list[str], walk: _Walk
) -> None:
    for task in _tasks(tasks):
        where = _rel(tree, path)
        for module in SET_FACT & task.keys():
            for name, value in (task[module] or {}).items():
                if name == "cacheable":
                    continue
                walk.set_by_tasks.setdefault(name, set()).add(where)
                walk.set_values.setdefault(name, []).append((where, value))
        if isinstance(task.get("register"), str):
            walk.set_by_tasks.setdefault(task["register"], set()).add(where)
        for module in INCLUDE_TASKS & task.keys():
            args = task[module]
            target = args.get("file") if isinstance(args, dict) else args
            _walk_file(tree, _include_file(tree, path, str(target)), tags, walk)
        for module in INCLUDE_ROLE & task.keys():
            for tasks_file in _role_tasks(tree, path, task[module], tags):
                included = _role_of(tree, tasks_file)
                assert included is not None
                walk.include_vars.setdefault(included, []).append(
                    (_role_of(tree, path), dict(task.get("vars") or {}))
                )
                _walk_file(tree, tasks_file, tags, walk)


def walk_play(tree: Tree, tags: list[str]) -> _Walk:
    """Every task file the k8s play reaches, and what its tasks set.

    Raises:
        CensusError: `deploy.yml` holds no k8s play, or an include names no file.
    """
    deploy = tree.ansible / "deploy.yml"
    plays = [
        p for p in yaml_fast.safe_load(deploy.read_text()) if p.get("name") == K8S_PLAY
    ]
    if len(plays) != 1:
        raise CensusError(f"{deploy} holds {len(plays)} plays named {K8S_PLAY!r}")
    walk = _Walk()
    for section in ("pre_tasks", "tasks", "post_tasks"):
        _walk_tasks(tree, deploy, plays[0].get(section) or [], tags, walk)
    return walk


def _values(value: object) -> Iterator[str]:
    """Every string in a YAML value, the keys of a mapping included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _values(k)
            yield from _values(v)
    elif isinstance(value, list):
        for v in value:
            yield from _values(v)


def _mapping(path: Path) -> dict:
    loaded = yaml_fast.safe_load(path.read_text()) if path.is_file() else None
    return loaded if isinstance(loaded, dict) else {}


def definitions(tree: Tree, walk: _Walk) -> dict[str, list[tuple[str, object]]]:
    """Each name mapped to every value that defines it, with the file that holds the value."""
    found: dict[str, list[tuple[str, object]]] = {}

    def add(where: Path | str, mapping: dict) -> None:
        for name, value in mapping.items():
            found.setdefault(str(name), []).append((str(where), value))

    for role in sorted(walk.roles):
        for layer in ("defaults", "vars"):
            for path in sorted((role / layer).glob("*.yml")):
                add(_rel(tree, path), _mapping(path))
    inventory = tree.ansible / "inventory"
    for path in sorted((inventory / "group_vars").glob("*.yml")):
        add(_rel(tree, path), _mapping(path))
    add(
        f"inventory/host_vars/{tree.host}.yml",
        _mapping(inventory / "host_vars" / f"{tree.host}.yml"),
    )
    for role, sites in sorted(walk.include_vars.items()):
        for caller, task_vars in sites:
            add(
                f"include_role {_rel(tree, role)} from {caller and _rel(tree, caller)}",
                task_vars,
            )
    for name, values in walk.set_values.items():
        for where, value in values:
            add(f"set_fact in {where}", {name: value})
    return found


def tag_templates(
    tree: Tree, tag: str, walk: _Walk, entry: dict
) -> set[tuple[Path, Path]]:
    """The templates the render reads for `tag`, each with the role templates dir it loads under.

    `resolved_manifest_files` decides the file list, so a one-off `-job.yaml` is outside it.
    The role's deferred files, its image contexts and the tree's `always` templates join it.
    """
    role = tree.roles / "k8s" / tag
    templates = role / "templates"
    files, secret = resolved_manifest_files(tag, tree.roles / "k8s", entry)
    sources = set()
    for name in files | secret | _deferred(role):
        own = templates / f"{name}.j2"
        if own.is_file():
            sources.add(own)
        elif name in SHARED_MANIFEST_DEFAULTS:
            sources.add(tree.shared / SHARED_MANIFEST_DEFAULTS[name])
    found = {(path, templates) for path in [*sources, *tree.always]}
    builder = tree.roles / IMAGE_BUILDER
    for caller, site in walk.include_vars.get(builder, []):
        if caller != role:
            continue
        found.add((builder / "templates" / CONTEXT_TEMPLATE, builder / "templates"))
        for key in CONTEXT_VARS:
            value = site.get(key) or {}
            for path in [value] if isinstance(value, str) else list(value):
                resolved = str(path).replace("{{ playbook_dir }}", str(tree.ansible))
                found.add((Path(resolved), templates))
    return found


def _deferred(role: Path) -> set[str]:
    """The `manifests_deferred_files` basenames `role`'s tasks pass to its manifests include."""
    names: set[str] = set()
    for path in sorted((role / "tasks").glob("*.yml")):
        for task in _tasks(yaml_fast.safe_load(path.read_text())):
            for value in _values(
                (task.get("vars") or {}).get("manifests_deferred_files") or []
            ):
                names |= {
                    n for n in value.replace(",", " ").split() if n.endswith(".yaml")
                }
    return names


def census(
    tree: Tree, tags: list[str], derived: frozenset[str], entries: dict[str, dict]
) -> Census:
    """Run the census over `tags` of `tree`.

    Args:
        tree: The Ansible tree.
        tags: The render targets, each a role under `roles/k8s/`.
        derived: The names the renderer computes from the tree.
        entries: Each tag's `containers_list` entry, which decides its shared defaults.

    Raises:
        CensusError: The play walk cannot resolve an include.
    """
    walk = walk_play(tree, tags)
    result = Census(set_by_tasks=walk.set_by_tasks, derived=derived)
    host_vars = _mapping(tree.ansible / "inventory" / "host_vars" / f"{tree.host}.yml")
    containers = filter_by_platform(host_vars.get("containers_list") or [], "k8s")
    reader = Reader(tree.ansible, containers)
    pending: list[tuple[str, str]] = []

    for tag in tags:
        queue = sorted(tag_templates(tree, tag, walk, entries.get(tag, {})))
        while queue:
            path, role_templates = queue.pop()
            if path in result.templates:
                continue
            result.templates.add(path)
            where = _rel(tree, path)
            names, ast = reader.read(where, path.read_text())
            pending += [(name, where) for name in names]
            for name in [*references(ast), *template_lookups(tree.ansible, ast)]:
                # A relative name resolves as the role's loader resolves it.
                for base in (role_templates, tree.shared):
                    candidate = base / name
                    if candidate.is_file():
                        queue.append((candidate, role_templates))
                        break

    defined = definitions(tree, walk)
    while pending:
        name, where = pending.pop()
        seen = name in result.reached
        result.reached.setdefault(name, set()).add(where)
        if seen or name in derived:
            continue
        for source, value in defined.get(name, []):
            for text in _values(value):
                if "{{" in text or "{%" in text:
                    names, _ = reader.read(f"{name} in {source}", text)
                    pending += [(n, f"{name} in {source}") for n in names]
    result.accepted, result.vars_names = reader.accepted, reader.vars_names
    result.problems = reader.problems
    return result


def derived_names(tags: list[str]) -> frozenset[str]:
    """The names the renderer computes for the repo's tree, as R3's *The census* lists them.

    Every name `task_inputs.derived_facts` returns and every `include_role` var
    `task_inputs.include_vars` returns for any tag, the secret keys and the sentinel, and the
    `containers_list` view `context.play_context` lays down.
    """
    stubs = shadow_diff.stub_vars()
    secrets = yaml_fast.safe_load(shadow_diff.SECRETS.read_text())
    names = {"containers_list", *stubs, *(str(k) for k in secrets if k != "sops")}
    for tag in tags:
        role = K8S_ROLES / tag
        names |= derived_facts(role, tag_context(role, tag, stubs)).keys()
        names |= include_vars(role).keys()
    return frozenset(names)


REAL_TREE = Tree(always=(CLAIM_TEMPLATE, LABEL_WRAPPER))


@functools.cache
def real_census() -> Census:
    """The census over the repo's tree and the k8s host's render targets."""
    tags = render_targets(K8S_HOST)
    return census(REAL_TREE, tags, derived_names(tags), k8s_entries())
