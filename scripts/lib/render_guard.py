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

The inventory readers at the bottom (``host_files``, ``k8s_services``) serve a wider set:
``deploy_tags.py`` and ``service_catalog.py`` both walked ``host_vars/`` and parsed the
service list themselves, and each carried its own copy of the ``_``-prefix exclusion.
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
from lib.git import git
from lib.ansible_inventory import K8S_HOST, containers_entries_in, host_names
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
    "containers_entries_in",
    "dump_numbered",
    "entry_tags",
    "entry_tags_at",
    "host_files",
    "host_layer",
    "k8s_services",
    "k8s_services_at",
    "load_yaml",
    "make_env",
    "pad_to_source_newlines",
    "render_or_error",
    "service_tags_at",
    "service_tags_at_or_none",
    "services_file",
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

    It stays chainable, so `hosts['x'].missing | default('y')` still renders its default,
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
    """Every host's ``host_vars`` file, in ``host_names`` order.

    The ``host_vars`` argument stays a parameter because every caller's tests inject a
    ``tmp_path`` through it.
    """
    return [host_vars / f"{name}.yml" for name in host_names(host_vars)]


def services_file(host_vars: Path) -> Path:
    """``cluster/services.yml`` in the tree whose ``ansible/inventory/host_vars`` is `host_vars`."""
    return host_vars.resolve().parents[2] / K8S_SERVICES.relative_to(REPO)


def host_layer(host_vars: Path, host: str) -> dict:
    """`host`'s host_vars file, ``{}`` for a host without one."""
    return load_yaml(host_vars / f"{host}.yml")


def k8s_services(path: Path = K8S_SERVICES) -> list[dict]:
    """The named entries of the service list `path`, every k8s service, in file order."""
    return containers_entries_in(load_yaml(path))


# host_vars as git names it: ``git show`` and ``git ls-tree`` address a path in a tree, not on
# disk.
HOST_VARS_IN_TREE = HOST_VARS.relative_to(REPO).as_posix()
SERVICES_IN_TREE = K8S_SERVICES.relative_to(REPO).as_posix()


def entry_tags(entry: dict) -> list[str]:
    """The tags one service entry selects under.

    An entry's own ``tags`` win, else its ``name`` alone. Every tag reader goes through here, or
    an entry that overrides its tags is validated against a name that no longer selects it. One
    definition, because ``deploy_tags.service_tags`` reads the working tree, and
    ``service_tags_at`` below reads a git ref.
    """
    return list(entry.get("tags") or [entry["name"]])


def k8s_services_at(ref: str, cwd: Path) -> list[dict]:
    """The named entries of ``cluster/services.yml`` AT ``ref``, read with git.

    A PR that adds a role and its service entry together is the case a checkout answers
    wrongly: the entry is in no tree until the tick fast-forwards, so the role reads as one
    nobody registered (``land_lib/classify.py`` carries the argument). Raises
    ``CalledProcessError`` on an unreadable ref or a ref without the file, and
    ``yaml_fast.UnparsableYaml`` on a file that does not load at ``ref``.
    """
    loaded = yaml_fast.parse_yaml(
        git("show", f"{ref}:{SERVICES_IN_TREE}", cwd=cwd).stdout
    )
    return containers_entries_in(loaded if isinstance(loaded, dict) else {})


def entry_tags_at(ref: str, cwd: Path) -> dict[str, set[str]]:
    """Entry name -> the tags that entry declares, AT ``ref``.

    The answer ``ctx.declared`` cannot give: ``service_tags_at`` returns a flat set of tag
    strings, so nothing in it says that an entry also declares a second tag, as ``n8n-images``
    did.
    """
    return {
        entry["name"]: set(entry_tags(entry)) for entry in k8s_services_at(ref, cwd)
    }


def service_tags_at(ref: str, cwd: Path) -> set[str]:
    """Every name that selects a service AT ``ref``."""
    return {tag for entry in k8s_services_at(ref, cwd) for tag in entry_tags(entry)}


def service_tags_at_or_none(ref: str, cwd: Path) -> set[str] | None:
    """``service_tags_at``, answering None for a read that cannot be trusted.

    AN EMPTY READ IS DAMAGE, NEVER EVIDENCE. An empty set says no service is declared, which
    for a caller validating deploy tags refuses every tag there is, and for one classifying
    roles marks every changed role unregistered. A ref this checkout cannot resolve raises
    inside ``git``, and an empty answer is indistinguishable from that here, so both become
    None and the caller falls back to the tree it can read. ``yaml_fast.UnparsableYaml`` is in
    the set because a service list that does not parse at ``ref`` is a damaged read like any
    other.
    """
    try:
        return service_tags_at(ref, cwd) or None
    except subprocess.SubprocessError, OSError, ValueError, yaml_fast.UnparsableYaml:
        return None
