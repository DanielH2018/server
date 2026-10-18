"""Which deploy tags a `secrets.yml` change reaches: the consumers of each key it moved.

`narrow` refused every range carrying `secrets.yml` until #5409, on the reasoning that a
rotated value reaches a service only when that service renders again. That holds, and it is
also why the refusal was wider than the change: the services that must render again are the
ones that read a key the range moved, and the key names are plaintext in the SOPS file. PR
#5405 added one key read only by `setup/claude_code`, and the refusal deferred all 55 k8s
services to `k8s_unapplied` for it.

SOPS re-encrypts only the values an edit changed, so the ciphertext of every other key is
byte-identical across the commit. Measured on the last eight commits to touch the file,
through 3ef888712, each changed exactly the keys its message names. A key added, rotated or removed is therefore a
key whose parsed value differs, and each one is mapped to its readers the way an inventory
key is: a grep across the role trees, the shared templates, the play and the inventory, at
the new ref. A setup-role reader maps to no deploy tag, because `deploy.yml` renders nothing
for it. A play-level reader, or a read through another inventory value, still refuses.

Two corrections over that grep come from `secrets_mgmt.consumers`, which asks the same
question of the whole tree, so the two answers cannot drift:

  - its skip list: `standalone_secret_keys.json` names about 150 keys and renders none, and
    a hit there would map to `manifests` and so to its 54 callers;
  - its cross-role `lookup()` readers: uptime-kuma renders every bridge push token out of
    monitor-bridge's `files/`, so a token rotated there reaches both tags.

The same keys reach the setup plane through `setup_reader_paths` (#5436), which the GitOps
tick runs through `secret_setup_readers.py`: a key `setup/claude_code` reads applies that role
there, because no deploy tag ever will.

The file is read with `git cat-file blob`, never `git show` or `git diff`. `.gitattributes`
gives it `diff=sops`, a textconv that decrypts, and no value may reach the journal.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_secret_keys.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/

from typing import Callable, NamedTuple

from lib.git import git
from lib.narrow_git import CannotNarrow, changed_mapping_keys, mapping_at
from lib.repo_paths import GITOPS_DEPLOY_FILES
from secrets_mgmt.consumers import _CENSUS_SKIP_FILES

_sys.path.insert(0, str(GITOPS_DEPLOY_FILES))

from deployer_lib.deploy_changes import _is_test_only_path, is_doc
from deployer_lib.deploy_cross_role import (
    SETUP_FILES_SHIPPED_BY_OTHER_ROLES,
    k8s_lookup_map,
)
from deploy_tools.narrow_lib.foreign_keys import keys_reaching
from deploy_tools.narrow_lib.setup_index import RoleIndex

SECRETS_FILE = "ansible/vars/secrets.yml"
SETUP_TREE = "ansible/roles/setup/"
# SOPS's own metadata — the MAC and `lastmodified` move on every edit and name no secret.
_SOPS_METADATA = "sops"


def _blob(ref: str, cwd) -> str:
    """`secrets.yml` at `ref` as raw ciphertext, or "" when the ref does not carry it."""
    try:
        r = git("cat-file", "blob", f"{ref}:{SECRETS_FILE}", cwd=cwd, check=False)
    except UnicodeDecodeError as exc:
        raise CannotNarrow(f"{SECRETS_FILE} is not text at {ref}") from exc
    return r.stdout if r.returncode == 0 else ""


def changed_keys(old_ref: str, new_ref: str, cwd) -> set[str]:
    """The secret names whose ciphertext differs between the two refs: added, rotated, removed."""
    old = mapping_at(_blob(old_ref, cwd), SECRETS_FILE)
    new = mapping_at(_blob(new_ref, cwd), SECRETS_FILE)
    old.pop(_SOPS_METADATA, None)
    new.pop(_SOPS_METADATA, None)
    return changed_mapping_keys(old, new, SECRETS_FILE)


class Rules(NamedTuple):
    """`broad`'s own rule functions, passed in because `broad` imports this module.

    An import back the other way, even one deferred into a function, is an import cycle
    `scripts/tests/test_scripts_import_direction.py` refuses.
    """

    grep: Callable
    sort_hits: Callable
    template_importers: Callable
    role_tags: Callable


def key_tags(key: str, ctx, lookups: dict[str, set[str]], rules: Rules) -> set[str]:
    """The deploy tags whose render reads the secret `key` at `ctx.ref`.

    `ctx` is `broad.Context`. `lookups` is `k8s_lookup_map`'s answer: a file another k8s role
    `lookup()`s, relative to `ansible/`, to the roles reading it.
    """
    hits = [
        h
        for h in rules.grep(ctx, key, word=True, inventory=True)
        if h.rsplit("/", 1)[-1] not in _CENSUS_SKIP_FILES
    ]
    found = rules.sort_hits(hits, f"the secret {key}", ctx, key)
    roles = set(found.roles)
    for hit in hits:
        roles |= lookups.get(hit.removeprefix("ansible/"), set())
    for name in sorted(found.templates):
        roles |= rules.template_importers(name, ctx.ref, ctx.cwd, {name}, ctx.explain)
    try:
        tags = rules.role_tags(roles, ctx)
    except CannotNarrow as exc:
        ctx.explain(f"narrow: secret {key} -> roles {','.join(sorted(roles))}")
        raise CannotNarrow(f"the secret {key} reaches a role where {exc}") from exc
    ctx.explain(f"narrow: secret {key} -> {','.join(sorted(tags)) or '(nothing)'}")
    return tags


def reached(old_ref: str, ctx, rules: Rules) -> set[str]:
    """Every deploy tag a `secrets.yml` change in `old_ref..ctx.ref` reaches."""
    keys = changed_keys(old_ref, ctx.ref, ctx.cwd)
    if not keys:
        return set()
    lookups = k8s_lookup_map(ctx.cwd)
    tags: set[str] = set()
    for key in sorted(keys):
        tags |= key_tags(key, ctx, lookups, rules)
    return tags


class SetupReaders(NamedTuple):
    """The setup-plane files reading a moved key, as `setup_reader_paths` finds them.

    Attributes:
        paths: every file under `roles/setup/` naming a key the range moved.
        unread: for a `defaults/` or `vars/` file in `paths` that other roles ship, the
            shippers that read none of its keys built from a moved secret. The deployer's
            `setup_roles_for` names every shipper, so it subtracts these.
    """

    paths: set[str]
    unread: dict[str, set[str]]


def setup_reader_paths(old_ref: str, ctx, rules: Rules) -> SetupReaders:
    """The files under `roles/setup/` at `ctx.ref` naming a key `old_ref..ctx.ref` moved (#5436).

    Paths rather than roles, so the deployer maps each through its own `setup_roles_for`, the
    edge that sends a file another role ships to that role too. A `.md`, a test file and a
    `_CENSUS_SKIP_FILES` name drop, as they do for `key_tags`. A shipped vars file is read
    only key by key, so its shippers are checked with `reads_through`: claude_code's defaults
    name four secrets, and `initial_setup` ships the file for one unrelated key.

    Raises:
        CannotNarrow: a key some reader outside the role trees names, where this grep cannot
            follow it to a setup role. That is a play-level file, an inventory value other
            than a comment (`rules.sort_hits` refuses both, as for the deploy plane), or a
            shared template, which a setup role may import. The tick then applies no setup
            role for the range and the `secrets` page stays its only signal.
    """
    found: set[str] = set()
    read_by: dict[str, set[str]] = {}
    for key in sorted(changed_keys(old_ref, ctx.ref, ctx.cwd)):
        outside = rules.sort_hits(
            [
                h
                for h in rules.grep(ctx, key, word=True, inventory=True)
                if h.rsplit("/", 1)[-1] not in _CENSUS_SKIP_FILES
            ],
            f"the secret {key}",
            ctx,
            key,
        )
        if outside.templates:
            raise CannotNarrow(
                f"the secret {key} is named by the shared template "
                f"{', '.join(sorted(outside.templates))}, which a setup role may import"
            )
        hits = readers(key, ctx.ref, ctx.cwd, [SETUP_TREE])
        ctx.explain(f"secret-readers: {key} -> {', '.join(hits) or '(no setup role)'}")
        found |= set(hits)
        for hit in hits:
            if not is_vars_file(hit):
                continue
            for role in SETUP_FILES_SHIPPED_BY_OTHER_ROLES.get(hit, ()):
                index = RoleIndex(role, ctx.ref, str(ctx.cwd))
                if reads_through(index, hit, key, ctx.ref, ctx.cwd):
                    read_by.setdefault(hit, set()).add(role)
    unread = {
        hit: set(SETUP_FILES_SHIPPED_BY_OTHER_ROLES[hit]) - read_by.get(hit, set())
        for hit in found
        if is_vars_file(hit) and hit in SETUP_FILES_SHIPPED_BY_OTHER_ROLES
    }
    return SetupReaders(found, {h: r for h, r in unread.items() if r})


def is_vars_file(path: str) -> bool:
    """Whether `path` is a role's `defaults/` or `vars/` file, read key by key."""
    return "/defaults/" in path or "/vars/" in path


def reads_through(index: RoleIndex, path: str, key: str, ref: str, cwd) -> bool:
    """Whether the role `index` reads the secret `key` through another role's vars file.

    The role reads such a file only by the keys it names, so it reads the secret when it
    names `key` or a key whose value is built from it (`keys_reaching`). A walk that cannot
    say counts as a read: a role applied too widely is slow, one left out holds a stale value.
    """
    try:
        names = keys_reaching(path, {key}, ref, str(cwd))
    except CannotNarrow:
        return True
    return any(index.mentions(name) for name in sorted(names))


def keys_named_in(pathspecs: list[str], old_ref: str, new_ref: str, cwd) -> list[str]:
    """The keys `old_ref..new_ref` moved that some file in `pathspecs` names at `new_ref`.

    `narrow_setup.role_tags` asks it for one role's own tree and the files it ships.
    """
    keys = changed_keys(old_ref, new_ref, cwd)
    return [k for k in sorted(keys) if readers(k, new_ref, cwd, pathspecs)]


def readers(key: str, ref: str, cwd, pathspecs: list[str]) -> list[str]:
    """The tracked paths in `pathspecs` at `ref` naming `key` as a word, sorted.

    A `.md`, a test file and a `_CENSUS_SKIP_FILES` name reach no host, so they drop.

    Raises:
        CannotNarrow: `git grep` failed.
    """
    r = git(
        "grep", "-l", "-w", "-F", "-e", key, ref, "--", *pathspecs,
        cwd=cwd, check=False,
    )  # fmt: skip
    if r.returncode > 1:
        raise CannotNarrow(f"`git grep {key}` failed: {r.stderr.strip()}")
    prefix = f"{ref}:"
    hits = (line[len(prefix) :] for line in r.stdout.splitlines() if line)
    return sorted(
        h
        for h in hits
        if not is_doc(h)
        and not _is_test_only_path(h)
        and h.rsplit("/", 1)[-1] not in _CENSUS_SKIP_FILES
    )
