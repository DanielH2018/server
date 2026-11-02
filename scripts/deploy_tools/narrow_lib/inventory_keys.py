"""Which setup-role files read an inventory key a range moved (#6962).

`deploy_changes` maps a path under `ansible/inventory/` to the deploy plane alone, and
`broad.narrow` maps each moved key to the deploy tags whose render reads it. Setup roles
template the same keys, and `ansible/deploy.yml` renders none of them. PR #6952 moved
`host_python_version`, which eight setup roles render into their units and crons, and the tick
applied none of them and recorded none in `manual_plane`.

The key diff is `broad._inventory_tags`'s: every top-level key whose parsed value differs in
an inventory YAML file the range touched. A `_`-prefixed file is loaded by no host, and
`hosts.ini` changes which hosts a play visits rather than a variable, so neither adds a key.

The readers come from `secret_keys.setup_readers_of`, the walk the `secrets.yml` keys take,
with two differences:

  - A file under a role's `files/` is copied verbatim, never templated, so naming a key there
    reads nothing. The deployer reads `containers_list` from the tree at run time, and
    counting its `files/` would re-apply `gitops_deploy` for every new service.
  - The deploy-plane refusals do not apply. `deploy.yml` reading `containers_list`, or a k8s
    macro reading an image pin, says nothing about a setup role, and refusing on them would
    have refused 17 of the last 40 inventory ranges. What still refuses is what can carry the
    key to a setup role unseen: a `hosts.ini` line naming it, and a shared template that a
    setup-role file names. An inventory value built from the key is followed instead, so its
    readers count too.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_inventory_keys.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/

import re

from deploy_tools.narrow_lib import secret_keys
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
    r = git(
        "diff", "--name-only", f"{old_ref}..{new_ref}", "--", INVENTORY,
        cwd=cwd, check=False,
    )  # fmt: skip
    if r.returncode != 0:
        raise CannotNarrow(f"`git diff -- {INVENTORY}` failed: {r.stderr.strip()}")
    keys: set[str] = set()
    for path in filter(_loaded, r.stdout.splitlines()):
        old = mapping_at(show_at(old_ref, path, cwd) or "", path)
        new = mapping_at(show_at(new_ref, path, cwd) or "", path)
        keys |= changed_mapping_keys(old, new, path)
    return keys


def carried(key: str, ref: str, cwd) -> set[str]:
    """`key`, and every inventory key at `ref` whose value is built from it, to a fixed point.

    Raises:
        CannotNarrow: `hosts.ini` names one of them, or a shared template a setup-role file
            names does.
    """
    found, pending = set(), [key]
    while pending:
        name = pending.pop()
        if name in found:
            continue
        found.add(name)
        word = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")
        for hit in secret_keys.readers(name, ref, cwd, [INVENTORY, SHARED_TEMPLATES]):
            if hit.startswith(SHARED_TEMPLATES):
                _refuse_if_setup_imports(hit, name, ref, cwd)
            elif _loaded(hit):
                values = mapping_at(show_at(ref, hit, cwd) or "", hit)
                pending += [k for k, v in values.items() if word.search(str(v))]
            elif not hit.rsplit("/", 1)[-1].startswith("_"):
                raise CannotNarrow(f"the variable {name} is named in {hit}")
    return found


def _refuse_if_setup_imports(template: str, key: str, ref: str, cwd) -> None:
    name = template.rsplit("/", 1)[-1]
    users = secret_keys.readers(name, ref, cwd, READER_PATHSPECS)
    if users:
        raise CannotNarrow(
            f"the variable {key} is named by the shared template {name}, which "
            f"{', '.join(users)} names"
        )


def setup_reader_paths(old_ref: str, ctx) -> secret_keys.SetupReaders:
    """The setup-role files at `ctx.ref` reading an inventory key `old_ref..ctx.ref` moved.

    `ctx` is `broad.Context`.

    Raises:
        CannotNarrow: every refusal `changed_keys`, `carried` and
            `secret_keys.setup_readers_of` make.
    """
    keys: set[str] = set()
    for key in sorted(changed_keys(old_ref, ctx.ref, ctx.cwd)):
        keys |= carried(key, ctx.ref, ctx.cwd)
    return secret_keys.setup_readers_of(keys, ctx, READER_PATHSPECS)


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
