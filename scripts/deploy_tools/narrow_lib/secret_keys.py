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

The file is read with `git cat-file blob`, never `git show` or `git diff`. `.gitattributes`
gives it `diff=sops`, a textconv that decrypts, and no value may reach the journal.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_secret_keys.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/

from typing import TYPE_CHECKING

from lib.git import git
from lib.narrow_git import CannotNarrow, changed_mapping_keys, mapping_at
from lib.repo_paths import GITOPS_DEPLOY_FILES
from secrets_mgmt.consumers import _CENSUS_SKIP_FILES

_sys.path.insert(0, str(GITOPS_DEPLOY_FILES))

from deployer_lib.deploy_cross_role import k8s_lookup_map

if TYPE_CHECKING:
    from deploy_tools.narrow_lib.broad import Context

SECRETS_FILE = "ansible/vars/secrets.yml"
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


def key_tags(key: str, ctx: "Context", lookups: dict[str, set[str]]) -> set[str]:
    """The deploy tags whose render reads the secret `key` at `ctx.ref`.

    `lookups` is `k8s_lookup_map`'s answer: a file another k8s role `lookup()`s, relative to
    `ansible/`, to the roles reading it.
    """
    # Lazy because `broad` imports this module.
    from deploy_tools.narrow_lib import broad

    hits = [
        h
        for h in broad._grep(ctx, key, word=True, inventory=True)
        if h.rsplit("/", 1)[-1] not in _CENSUS_SKIP_FILES
    ]
    found = broad._sort_hits(hits, f"the secret {key}", ctx, key)
    roles = set(found.roles)
    for hit in hits:
        roles |= lookups.get(hit.removeprefix("ansible/"), set())
    for name in sorted(found.templates):
        roles |= broad.template_importers(name, ctx.ref, ctx.cwd, {name}, ctx.explain)
    try:
        tags = broad._role_tags(roles, ctx)
    except CannotNarrow as exc:
        ctx.explain(f"narrow: secret {key} -> roles {','.join(sorted(roles))}")
        raise CannotNarrow(f"the secret {key} reaches a role where {exc}") from exc
    ctx.explain(f"narrow: secret {key} -> {','.join(sorted(tags)) or '(nothing)'}")
    return tags


def reached(old_ref: str, ctx: "Context") -> set[str]:
    """Every deploy tag a `secrets.yml` change in `old_ref..ctx.ref` reaches."""
    keys = changed_keys(old_ref, ctx.ref, ctx.cwd)
    if not keys:
        return set()
    lookups = k8s_lookup_map(ctx.cwd)
    tags: set[str] = set()
    for key in sorted(keys):
        tags |= key_tags(key, ctx, lookups)
    return tags
