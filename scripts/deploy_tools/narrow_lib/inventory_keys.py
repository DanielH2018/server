"""Which setup-role files read an inventory key a range moved (#6962).

`deploy_changes` maps a path under `ansible/inventory/` to the deploy plane alone. Setup roles
template the same keys, and `ansible/deploy.yml` renders none of them. PR #6952 moved
`host_python_version`, which eight setup roles render into their units and crons, and the tick
applied none of them and recorded none in `manual_plane`.

The key diff takes every top-level key whose parsed value differs in
an inventory YAML file the range touched. A `_`-prefixed file is loaded by no host, and
`hosts.ini` changes which hosts a play visits rather than a variable, so neither adds a key.

The readers come from `secret_keys.setup_readers_of`, the walk the `secrets.yml` keys take,
with two differences:

  - A file under a role's `files/` is copied verbatim, never templated, so naming a key there
    reads nothing. The deployer reads `containers_list` from the tree at run time, and
    counting its `files/` would re-apply `gitops_deploy` for every new service.
  - The deploy-plane refusals do not apply. `deploy.yml` reading `containers_list`, or a k8s
    macro reading an image pin, says nothing about a setup role, and refusing on them would
    have refused 17 of the last 40 inventory ranges. What could carry the key to a setup role
    unseen is followed instead (`carried`): an inventory value built from it, and a shared
    template naming it. So a moved key never refuses, and one refusal cannot leave a reader
    unrouted with nothing recorded.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_inventory_keys.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/

import re

from deploy_tools.narrow_lib import secret_keys
from deploy_tools.narrow_lib import templates as narrow_templates
from lib.git import git
from lib.narrow_git import CannotNarrow, changed_mapping_keys, mapping_at, show_at

INVENTORY = "ansible/inventory/"
SHARED_TEMPLATES = "ansible/templates/"
# Every setup-role file Ansible templates or parses, which is all of them but `files/`.
READER_PATHSPECS = [secret_keys.SETUP_TREE, ":(exclude)ansible/roles/setup/*/files/*"]


def _loaded(path: str) -> bool:
    """Whether a host loads `path` as a vars file."""
    name = path.rsplit("/", 1)[-1]
    return name.endswith((".yml", ".yaml")) and not name.startswith("_")


def changed_keys(old_ref: str, new_ref: str, cwd) -> set[str]:
    """The inventory variables whose value differs between the refs: added, changed, removed.

    Raises:
        CannotNarrow: `git diff` failed, or a touched inventory YAML file does not parse.
    """
    return set().union(*changed_keys_by_host(old_ref, new_ref, cwd).values())


def changed_keys_by_host(old_ref: str, new_ref: str, cwd) -> dict[str, set[str]]:
    """`changed_keys`, split by the one host a key's file reaches (#6968).

    A key moved in `host_vars/<h>.yml` is under `<h>`. Every other file's keys, the group
    files included, are under "", which stands for every host a reader's gate admits.

    Raises:
        CannotNarrow: `git diff` failed, or a touched inventory YAML file does not parse.
    """
    r = git(
        "diff", "--name-only", f"{old_ref}..{new_ref}", "--", INVENTORY,
        cwd=cwd, check=False,
    )  # fmt: skip
    if r.returncode != 0:
        raise CannotNarrow(f"`git diff -- {INVENTORY}` failed: {r.stderr.strip()}")
    keys: dict[str, set[str]] = {}
    for path in filter(_loaded, r.stdout.splitlines()):
        old = mapping_at(show_at(old_ref, path, cwd) or "", path)
        new = mapping_at(show_at(new_ref, path, cwd) or "", path)
        if moved := changed_mapping_keys(old, new, path):
            keys.setdefault(_host_of(path), set()).update(moved)
    return keys


def _host_of(path: str) -> str:
    """The host a `host_vars/<h>.yml` file is loaded by, "" for any other inventory file."""
    parent, _, name = path.rpartition("/")
    if parent == f"{INVENTORY}host_vars":
        return name.rsplit(".", 1)[0]
    return ""


def carried(key: str, ref: str, cwd) -> set[str]:
    """`key`, and every name at `ref` that carries its value onward, to a fixed point.

    Three kinds of name carry it: an inventory YAML key whose value names it, a `hosts.ini`
    inline variable whose value names it, and a shared template naming it. A template's file
    name is what a setup role names when it includes the macro, so the name walks on as a
    word like a key does, and the setup files naming it are its readers. A comment in
    `hosts.ini` names keys in prose (`server_ip`) and carries nothing.

    Raises:
        CannotNarrow: an inventory YAML file does not parse, or `git grep` failed.
    """
    found, pending = set(), [key]
    while pending:
        name = pending.pop()
        if name in found:
            continue
        found.add(name)
        word = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")
        hits = secret_keys.readers(name, ref, cwd, [INVENTORY, SHARED_TEMPLATES])
        # A Jinja comment naming a key or a macro renders nothing.
        for hit in narrow_templates.real_mentions(hits, name, ref, cwd):
            text = show_at(ref, hit, cwd) or ""
            if hit.startswith(SHARED_TEMPLATES):
                pending.append(hit.rsplit("/", 1)[-1])
            elif _loaded(hit):
                values = mapping_at(text, hit)
                pending += [k for k, v in values.items() if word.search(str(v))]
            elif not hit.rsplit("/", 1)[-1].startswith("_"):
                pending += _ini_vars_naming(text, word)
    return found


def _ini_vars_naming(text: str, word: re.Pattern) -> list[str]:
    """The inline `var=value` names on `hosts.ini`'s host lines whose value matches `word`."""
    names = []
    for line in text.splitlines():
        if line.lstrip().startswith(("#", ";", "[")):
            continue
        # A value runs to the next `var=`, since `{{ key }}` holds spaces.
        starts = list(re.finditer(r"(\w+)=", line))
        ends = [m.start() for m in starts[1:]] + [len(line)] if starts else []
        for here, end in zip(starts, ends, strict=True):
            if word.search(line[here.end() : end]):
                names.append(here.group(1))
    return names


def setup_reader_paths(old_ref: str, ctx) -> secret_keys.SetupReaders:
    """The setup-role files at `ctx.ref` reading an inventory key `old_ref..ctx.ref` moved.

    `ctx` is `role_reads.Context`.

    Raises:
        CannotNarrow: an inventory YAML file does not parse, or `git` failed. Neither is an
            answer about a reader, so the tick parks and retries rather than route nothing.
    """
    return secret_keys.setup_readers_of(
        _carried_all(changed_keys(old_ref, ctx.ref, ctx.cwd), ctx),
        ctx,
        READER_PATHSPECS,
    )


def setup_reader_paths_by_host(
    old_ref: str, ctx
) -> dict[str, secret_keys.SetupReaders]:
    """`setup_reader_paths`, split by the host each moved key's file reaches (#6968).

    The keys are split by `changed_keys_by_host`, and a key carried onward keeps its origin
    file's host: a `group_vars` value built from a `host_vars/<h>.yml` key still differs on
    `<h>` alone.

    Raises:
        CannotNarrow: as `setup_reader_paths`.
    """
    return {
        host: secret_keys.setup_readers_of(
            _carried_all(keys, ctx), ctx, READER_PATHSPECS
        )
        for host, keys in sorted(
            changed_keys_by_host(old_ref, ctx.ref, ctx.cwd).items()
        )
    }


def _carried_all(keys: set[str], ctx) -> set[str]:
    """`carried` over each of `keys`, as one set."""
    found: set[str] = set()
    for key in sorted(keys):
        found |= carried(key, ctx.ref, ctx.cwd)
    return found


def keys_named_in(pathspecs: list[str], old_ref: str, new_ref: str, cwd) -> list[str]:
    """The inventory keys `old_ref..new_ref` moved that a templated file in `pathspecs` names.

    `narrow_setup.role_tags` asks it for one role's own tree and the files it ships, as it asks
    `secret_keys.keys_named_in` for the secrets.
    """
    keys: set[str] = set()
    for key in changed_keys(old_ref, new_ref, cwd):
        keys |= carried(key, new_ref, cwd)
    specs = [*pathspecs, *READER_PATHSPECS[1:]]
    return [k for k in sorted(keys) if secret_keys.readers(k, new_ref, cwd, specs)]
