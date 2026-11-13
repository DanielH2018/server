#!/usr/bin/env python3
"""Shared helpers for the render-guard scripts and other Ansible-inventory readers.

Used by ``validate/compose_templates.py``, ``validate/k8s_manifests.py``, and
``validate/shell_templates.py``, plus the other scripts that read the same Ansible
inventory.

Each render guard renders Jinja templates with stubbed variables and asserts the output is valid
(YAML for the first two, shell for the third). The pieces that are identical across all three —
the repo path anchors, the non-secret fallback context, the plaintext-YAML loader, the numbered-
source dumper, and the base stub-undefined class — live here so they stay in sync instead of
being hand-copied.

The inventory readers at the bottom (``host_files``, ``containers_entries``) serve a wider set:
``deploy_tags.py`` and ``service_catalog.py`` both walked ``host_vars/`` and parsed
``containers_list`` themselves, and each carried its own copy of the ``_``-prefix exclusion.
The path anchors come from ``repo_paths.py`` and are re-exported here for the callers that
already import them from this module; a script that wants only a path imports
``repo_paths`` directly and skips the ``jinja2`` import this module costs.

Imported as ``from lib.render_guard import ...`` after the caller's own ``sys.path``
bootstrap puts ``scripts/`` on the path (`.claude/rules/python-layout.md`).
"""

import copy
import functools
import subprocess
import sys
from pathlib import Path

from jinja2 import ChainableUndefined, Environment, FileSystemLoader, StrictUndefined

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import yaml_fast
from lib.git import git, git_stdout
from lib.ansible_inventory import K8S_HOST, containers_entries_in
from lib.repo_paths import (
    ALL_VARS,
    ANSIBLE,
    HOST_VARS,
    INVENTORY,
    K8S_SERVICES,
    REPO,
    SHARED_TPL,
)

__all__ = [
    "ALL_VARS",
    "ANSIBLE",
    "BASE_CONTEXT",
    "BUILT_IMAGE_TAG_STUBS",
    "HOST_VARS",
    "HOST_VARS_IN_TREE",
    "INVENTORY",
    "K8S_HOST",
    "REPO",
    "SERVICES_IN_TREE",
    "SHARED_TPL",
    "StrictChainableUndefined",
    "StubUndefined",
    "containers_entries",
    "containers_entries_at",
    "containers_entries_in",
    "dump_numbered",
    "entry_platform",
    "entry_tags",
    "entry_tags_at",
    "host_files",
    "host_layer",
    "load_yaml",
    "make_env",
    "pad_to_source_newlines",
    "render_or_error",
    "service_records_at",
    "service_records_at_or_none",
    "service_tags_at",
    "service_tags_at_or_none",
    "services_file",
    "with_services",
]

# k8s/image-builder publishes `k8s_built_image_tags` as a play fact at deploy time (image name ->
# content tag), and every built-image pin reads it as `k8s_built_image_tags.get('<name>',
# 'latest')`. A render here has no play, so without a seed the fallback fires: every
# rendered-manifest guard would check `:latest` while production renders the hash — passing, and
# no longer reading the ref the cluster gets. The digests are arbitrary; their SHAPE and this
# map's keys are asserted by ansible/tests/k8s/test_built_images_name_the_content_tag.py.
BUILT_IMAGE_TAG_STUBS: dict[str, str] = {
    "code-server": "sha-c0de5e2e7000",
    "homelab-mcp": "sha-70c1ab0e1111",
    "ical-proxy": "sha-1ca17e2d2222",
    "karakeep": "sha-ca4a6eef9999",
    "n8n": "sha-8a8a8a8a3333",
    "n8n-runners": "sha-8b8b8b8b4444",
    "nut": "sha-4a7f6d0e5555",
    "pi-peer-backup": "sha-b0cca17e6666",
    "terraria": "sha-7e44a121a777",
    "valheim": "sha-a1de17ab8888",
}

# Non-secret fallbacks for values `group_vars/all.yml` does not hold. Anything still missing
# (SOPS secrets, role vars) renders via StubUndefined — fine for a STRUCTURAL parse/lint check.
BASE_CONTEXT = {
    "email": "stub@example.com",
    "server_ip": "10.0.0.1",
    "k8s_built_image_tags": BUILT_IMAGE_TAG_STUBS,
}


# DECIDED: secret values render as stubs, so a rendered-manifest guard must key on field names
# and assert a non-empty denominator, never on credential-shaped values.
class StubUndefined(ChainableUndefined):
    """A Jinja undefined value that renders as the literal ``STUB`` instead of raising.

    Used for any undefined variable — a SOPS secret, a host fact — so structural
    rendering never aborts on a missing value. Tolerates attribute/item access,
    iteration, and concatenation (Jinja's ``indent`` filter prepends a newline via
    ``+``, so ``{{ secret | indent(n) }}`` needs ``__add__``).
    """

    _FILL = "STUB"

    def __str__(self) -> str:  # {{ secret }}
        return self._FILL

    def __iter__(self):  # {% for x in undefined %}
        return iter(())

    def __add__(self, other):  # {{ secret | indent(n) }}
        return self._FILL + str(other)

    def __radd__(self, other):
        return str(other) + self._FILL


class StrictChainableUndefined(ChainableUndefined, StrictUndefined):
    """An undefined that raises when it is printed, iterated or tested, as Ansible's does.

    It stays chainable, so `hostvars['x'].missing | default('y')` still renders its default,
    as it does under Ansible. `homelab-render` renders with it, where the guards keep
    `StubUndefined`.
    """


def load_yaml(path: Path) -> dict:
    """A YAML mapping, or ``{}`` for a missing, empty or non-mapping file.

    Every caller indexes the result as a dict, so a file whose top level is a list or a
    scalar returns ``{}`` rather than leaking through to a ``TypeError`` several calls later.

    Raises:
        yaml_fast.UnparsableYaml: the file does not load.
    """
    if not path.is_file():
        return {}
    # A copy, because callers build context dicts from the values and must not reach the
    # cached one.
    return copy.deepcopy(_parsed_mapping(path.read_text()))


# Keyed on the text rather than the path, so an edit between two renders reaches the second
# one even inside the same mtime tick. Reading the file costs little; parsing it is what a
# render paid for again on every render_context call (#5592).
@functools.lru_cache(maxsize=512)
def _parsed_mapping(text: str) -> dict:
    loaded = yaml_fast.parse_yaml(text)
    return loaded if isinstance(loaded, dict) else {}


def dump_numbered(text: str) -> None:
    for i, line in enumerate(text.splitlines(), 1):
        print(f"  {i:3d}| {line}", file=sys.stderr)


def make_env(dirs=(), undefined_cls=StubUndefined) -> Environment:
    """The one Jinja Environment factory the render guards and the renderer build on.

    Carries the given template dirs on the loader, the stub-undefined class, and the whitespace
    flags matching Ansible's Templar so rendered output matches a real deploy. It registers no
    filter: ``lib.ansible_jinja_env.register_ansible_filters(make_env(...))`` adds the Ansible
    ones, and ``template_env`` does that over a template directory and ``ansible/templates/``.

    With no ``dirs`` the environment has an empty loader, which is the form a test takes to
    render a template's text or a task's expression through ``from_string``. A bare
    ``jinja2.Environment()`` has neither the whitespace flags nor the filters, and the
    ``tests-build-no-bare-jinja-environment`` row of
    ``scripts/tests/test_census_rows_test_renders.py`` refuses one in a test.
    """
    return Environment(
        loader=FileSystemLoader([str(d) for d in dirs]),
        undefined=undefined_cls,
        trim_blocks=True,
        lstrip_blocks=False,
        keep_trailing_newline=True,
    )


def pad_to_source_newlines(source: str, text: str) -> str:
    """`text`, padded with newlines until it ends in as many as `source` does.

    This is ansible-core's `TemplateEngine._post_render_mutation`, which the template module,
    the template lookup and var resolution all apply. It pairs with an environment whose
    `keep_trailing_newline` is False: Jinja drops the source's last newline, and the engine
    appends only the newlines the render comes up short. A render that already ends in enough
    keeps its own, so the label wrapper's closing `}}` line adds none after the YAML dump's.
    """
    missing = _newlines_at_end(source) - _newlines_at_end(text)
    return text + "\n" * missing if missing > 0 else text


def _newlines_at_end(text: str) -> int:
    return len(text) - len(text.rstrip("\n"))


def render_or_error(
    env: Environment, name: str, ctx: dict
) -> tuple[str | None, str | None]:
    """Render template ``name`` with ``ctx`` (also injected as globals so imported macros see it).

    Return ``(rendered, None)`` on success or ``(None, "render error: <Type>: <msg>")`` — the
    identical error format all three guards wrapped their render call in.
    """
    env.globals.update(ctx)
    try:
        return env.get_template(name).render(**ctx), None
    except Exception as exc:
        return None, f"render error: {type(exc).__name__}: {exc}"


def host_files(host_vars: Path = HOST_VARS) -> list[Path]:
    """Every real host's ``host_vars`` file, sorted.

    ``_example.yml`` is a template for a new host, not a host — Ansible only loads a host_vars
    file whose name matches an inventory hostname, so it is inert to a real run and must be
    inert here too. ``ansible/tests/deploy/test_containers_list_roles_exist.py`` makes the same
    exclusion. The ``host_vars`` argument stays a parameter because every caller's tests inject a
    ``tmp_path`` through it.
    """
    return sorted(p for p in host_vars.glob("*.yml") if not p.name.startswith("_"))


def services_file(host_vars: Path) -> Path:
    """``cluster/services.yml`` in the tree whose ``ansible/inventory/host_vars`` is `host_vars`."""
    return host_vars.resolve().parents[2] / K8S_SERVICES.relative_to(REPO)


def with_services(host: str, layer: dict, services: dict) -> dict:
    """`layer` with the k8s host's ``containers_list`` taken from the parsed service list.

    Only ``K8S_HOST`` gains the list, and only when `services` declares it, so a tree from
    before the list left ``host_vars`` reads as it did.

    Raises:
        ValueError: the k8s host's host_vars file declares ``containers_list`` as well.
    """
    if host != K8S_HOST or "containers_list" not in services:
        return layer
    if "containers_list" in layer:
        raise ValueError(
            f"{K8S_HOST}'s host_vars and {K8S_SERVICES.relative_to(REPO)} both declare "
            "containers_list; the k8s services live in the second alone"
        )
    return {**layer, "containers_list": services["containers_list"]}


def host_layer(host_vars: Path, host: str) -> dict:
    """`host`'s host_vars file as the renderer reads it, ``{}`` for a host without one.

    The k8s host's ``containers_list`` comes from ``cluster/services.yml`` beside the
    inventory (`with_services`), so every reader of that host's layer sees the k8s services
    under the variable name the templates use.
    """
    layer = load_yaml(host_vars / f"{host}.yml")
    if host != K8S_HOST:
        return layer
    return with_services(host, layer, load_yaml(services_file(host_vars)))


def containers_entries(path: Path) -> list[dict]:
    """The named ``containers_list`` entries of the host whose host_vars file is `path`."""
    return containers_entries_in(host_layer(path.parent, path.stem))


# host_vars as git names it: ``git show`` and ``git ls-tree`` address a path in a tree, not on
# disk.
HOST_VARS_IN_TREE = HOST_VARS.relative_to(REPO).as_posix()
SERVICES_IN_TREE = K8S_SERVICES.relative_to(REPO).as_posix()


def entry_tags(entry: dict) -> list[str]:
    """The tags one ``containers_list`` entry selects under.

    An entry's own ``tags`` win, else its ``name`` alone. Every tag reader goes through here, or
    an entry that overrides its tags is validated against a name that no longer selects it. One definition, because ``deploy_tags.service_tags`` reads the working
    tree, and ``service_tags_at`` below reads a git ref.
    """
    return list(entry.get("tags") or [entry["name"]])


def entry_platform(entry: dict) -> str:
    """The platform one ``containers_list`` entry deploys on: ``k8s`` or ``docker``.

    No host_vars file sets ``platform`` on a docker entry (daniel-pi's don't carry the key at
    all) -- k8s is the one that's always explicit, so docker is the default rather than an
    unlabelled third state. One definition, because ``land_lib/health_gate.tag_platforms`` reads the
    working tree and ``service_records_at`` below reads a git ref.
    """
    return entry.get("platform", "docker")


def service_records_at(ref: str, cwd: Path) -> list[tuple[str, str, str]]:
    """``(host, platform, tag)`` for every ``containers_list`` entry AT ``ref``, read with git.

    A PR that adds a role and its ``containers_list`` entry together is the case a checkout
    answers wrongly: the entry is in no tree until the tick fast-forwards, so the role reads as
    one nobody registered (``land_lib/classify.py`` carries the argument), and a
    new Pi role reads as declared on NO host, so its first landing deploys locally instead of
    with ``-e target=daniel-pi``. Names are listed at ``ref`` too, so a host_vars
    file the same PR adds counts, and ``_example.yml`` is excluded for the reason
    ``host_files`` excludes it. The host is the file's stem, as ``deploy_tags.service_records``
    names it from a path.

    Reuses ``entry_tags``, ``entry_platform`` and ``containers_entries_in`` rather than
    re-reading a containers_list entry its own way: two derivations of "which tags exist" that
    disagree is exactly the defect this answer is meant to fix. Raises ``CalledProcessError``
    on an unreadable ref.
    """
    return [
        (host, entry_platform(entry), tag)
        for host, entry in containers_entries_at(ref, cwd)
        for tag in entry_tags(entry)
    ]


def containers_entries_at(ref: str, cwd: Path) -> list[tuple[str, dict]]:
    """``(host, entry)`` for every named ``containers_list`` entry AT ``ref``, read with git.

    The read ``service_records_at`` above is expressed on, kept separate because that one
    flattens an entry to its tags and a caller asking which tags ONE entry declares cannot
    recover the grouping from the flattened form. Raises ``CalledProcessError`` on an
    unreadable ref, as its caller's docstring says, and ``yaml_fast.UnparsableYaml`` on a file
    that does not load at ``ref``.
    """
    services = (
        _mapping_at(ref, SERVICES_IN_TREE, cwd)
        if git_stdout("ls-tree", "--name-only", ref, "--", SERVICES_IN_TREE, cwd=cwd)
        else {}
    )
    entries: list[tuple[str, dict]] = []
    for name in git_stdout(
        "ls-tree", "--name-only", f"{ref}:{HOST_VARS_IN_TREE}", cwd=cwd
    ).splitlines():
        if not name.endswith(".yml") or name.startswith("_"):
            continue
        host = name.removesuffix(".yml")
        layer = with_services(
            host, _mapping_at(ref, f"{HOST_VARS_IN_TREE}/{name}", cwd), services
        )
        for entry in containers_entries_in(layer):
            entries.append((host, entry))
    return entries


def _mapping_at(ref: str, path: str, cwd: Path) -> dict:
    loaded = yaml_fast.parse_yaml(git("show", f"{ref}:{path}", cwd=cwd).stdout)
    return loaded if isinstance(loaded, dict) else {}


def entry_tags_at(ref: str, cwd: Path) -> dict[str, set[str]]:
    """Entry name -> the tags that entry declares, for every host's list AT ``ref``.

    The answer ``ctx.declared`` cannot give: ``service_tags_at`` returns a flat set of tag
    strings, so nothing in it says that an entry also declares a second tag, as ``n8n-images`` did. Two
    hosts declaring the same name is not a shape this inventory has; the tags are unioned
    rather than raising, because a union only ever makes a caller's answer more cautious.
    """
    tags: dict[str, set[str]] = {}
    for _host, entry in containers_entries_at(ref, cwd):
        tags.setdefault(entry["name"], set()).update(entry_tags(entry))
    return tags


def service_records_at_or_none(
    ref: str, cwd: Path
) -> list[tuple[str, str, str]] | None:
    """``service_records_at``, answering None for a read that cannot be trusted.

    AN EMPTY READ IS DAMAGE, NEVER EVIDENCE. ``[]`` says no service is declared anywhere,
    which for a caller validating deploy tags refuses every tag there is; for one classifying
    roles it marks every changed role unregistered; for one routing tags to
    hosts it routes every tag to no host. A ref this checkout cannot resolve raises inside
    ``git``, and an empty answer is indistinguishable from that here, so both become None and
    the caller falls back to the tree it can read.

    ``yaml_fast.UnparsableYaml`` is in the set because the read parses YAML fetched from ``ref``: a
    host_vars file that does not parse at that commit is a damaged read like any other, and
    letting it escape turns a read of ``ref`` into a traceback for a file
    that is merely unparseable.
    """
    try:
        return service_records_at(ref, cwd) or None
    except subprocess.SubprocessError, OSError, ValueError, yaml_fast.UnparsableYaml:
        return None


def service_tags_at(ref: str, cwd: Path) -> set[str]:
    """Every name that selects a service AT ``ref``: ``service_records_at`` flattened to tags."""
    return {tag for _host, _platform, tag in service_records_at(ref, cwd)}


def service_tags_at_or_none(ref: str, cwd: Path) -> set[str] | None:
    """``service_tags_at`` under ``service_records_at_or_none``'s damage rule: None, never ``set()``."""
    records = service_records_at_or_none(ref, cwd)
    return None if records is None else {tag for _host, _platform, tag in records}
