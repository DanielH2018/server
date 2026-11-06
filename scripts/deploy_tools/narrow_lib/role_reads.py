#!/usr/bin/env python3
"""Which roles read a name: the grep, the hit sort and the tag mapping the key readers share.

`secret_keys` and `inventory_keys` ask this of each key a range moved. `land_lib/key_readers.py`
and `secret_setup_readers.py` hand it to them, for a landing and for the tick.

  - `grep` lists the tracked files at a ref that name a key or a shared template.
  - `sort_hits` splits those into role directories and shared templates. A hit under a play
    path or in another inventory value refuses.
  - `template_importers` follows a shared template to the roles that import or include it,
    through a macro another macro imports.
  - `role_tags` maps role directories to the deploy tags that select them.

ANY DOUBT IS A REFUSAL, naming every candidate, rather than a guess at a narrower set (#4333).

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_secret_keys.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/

import re
from pathlib import Path
from typing import Callable, NamedTuple

from deploy_tools.narrow_lib import filters
from deploy_tools.narrow_lib import templates as narrow_templates
from lib import yaml_fast
from lib.git import git
from lib.narrow_git import CannotNarrow, show_at
from lib.render_guard import service_tags_at
from lib.repo_paths import GITOPS_DEPLOY_FILES

# The deployer's own `files/`, so there is one path mapper, not two. Imported at module level
# because `deploy_changes` is stdlib-only and imports nothing from this tree, so no cycle opens.
_sys.path.insert(0, str(GITOPS_DEPLOY_FILES))

from deployer_lib.deploy_changes import _BROAD_PLAY_PREFIXES
from deployer_lib.deploy_changes import _is_test_only_path, role_of

# Paths whose content every play reads, so no `--tags` value scopes a change to them: the play
# itself, the task directories it imports, the toposort, and ansible.cfg.
PLAY_PREFIXES = _BROAD_PLAY_PREFIXES
# The trees a variable or a macro can be consumed from and still map to a deploy tag.
# `ansible/roles/setup/` is absent on purpose: `secret_keys` and `inventory_keys` walk the setup
# plane themselves, and `ansible/deploy.yml` renders nothing for it.
ROLE_TREES = ("ansible/roles/k8s",)
SHARED_TEMPLATES = "ansible/templates/"
INVENTORY = "ansible/inventory/"


class Importers(NamedTuple):
    """What a grep hit list holds: the role directories, and the shared templates.

    The two are answered together because a hit under `ansible/templates/` is not an answer
    yet — it is a second question, asked of that template's own importers.
    """

    roles: set[str]
    templates: set[str]


class Context(NamedTuple):
    """What a rule needs besides the range: the tree to read and the tags that exist.

    `declared` is read AT THE NEW REF rather than from the working tree, so a range that
    registers a new service maps that service's own paths to its own tag. The deployer calls
    this before its fast-forward, where the working tree is still one commit behind.
    `callers` is the k8s role-caller graph, which only a working tree can be walked for.
    """

    cwd: Path
    ref: str
    declared: set[str]
    callers: dict[str, set[str]]
    explain: Callable[[str], None]


def grep(
    ctx: Context, pattern: str, *, word: bool, inventory: bool = False
) -> list[str]:
    """Every tracked path at `ctx.ref` whose content contains `pattern`, as a fixed string.

    `inventory` adds the inventory tree, which a variable scan needs and a macro scan does
    not. A variable can be consumed by another variable — `foo: "{{ bar }}"` — and the key
    diff cannot see that, because `foo`'s own parsed value did not change when `bar` did.
    Every key also hits its own definition line, so `sort_hits` tells the two apart.
    """
    args = ["grep", "-l", "-F"]
    if word:
        args.append("-w")
    args += [
        "-e",
        pattern,
        ctx.ref,
        "--",
        *ROLE_TREES,
        SHARED_TEMPLATES,
        *PLAY_PREFIXES,
        *((INVENTORY,) if inventory else ()),
    ]
    r = git(*args, cwd=ctx.cwd, check=False)
    if r.returncode > 1:
        raise CannotNarrow(f"`git grep {pattern}` failed: {r.stderr.strip()}")
    prefix = f"{ctx.ref}:"
    return [line[len(prefix) :] for line in r.stdout.splitlines() if line]


def _defines_only(key: str, path: str, ctx: Context) -> bool:
    r"""True when `path` mentions `key` only on its own top-level definition line.

    Every key hits the file that defines it, and that hit is not a consumer. Anything else
    in an inventory file is one, and one the key diff cannot see: the consuming key's own
    parsed value is unchanged. `\w` matches `git grep -w`'s own boundary, so this sees every
    hit the grep saw.

    A line that is wholly a comment is skipped: Ansible parses none of it, so it consumes
    nothing, and `group_vars/all.yml` documents its own keys by name. Counting those refused
    22 of its 87 top-level keys rather than 8. A TRAILING comment on a value line still
    counts, because telling a real `#` from one inside a quoted value needs a parse.

    The one unsafe direction the whole-line rule leaves: a continuation line of a block
    scalar (`key: |`) that begins with `#` is content Ansible does read, and this skips it,
    so a key consumed only there would narrow rather than refuse. No block scalar in
    `ansible/inventory/` has such a line, and the one that appears is a reason to parse
    instead. ENFORCED: `ansible/tests/deploy/
    test_inventory_block_scalars_have_no_comment_shaped_lines.py` fails on the first one.
    """
    defines = re.compile(rf"^{re.escape(key)}\s*:")
    mentions = re.compile(rf"(?<!\w){re.escape(key)}(?!\w)")
    for line in (show_at(ctx.ref, path, ctx.cwd) or "").splitlines():
        if line.lstrip().startswith("#"):
            continue
        if mentions.search(line) and not defines.match(line):
            return False
    return True


def sort_hits(
    hits: list[str], subject: str, ctx: Context, key: str | None = None
) -> Importers:
    """Split grep hits into roles and shared templates; a play-level hit refuses.

    A `.md` and a test file (the deployer's `_is_test_only_path`, #3660) reach no host, so
    both drop. An inventory hit that is not `key`'s own definition refuses: see
    `_defines_only`. A `_`-prefixed inventory file is exempt on both sides, as a file no host
    loads, so its commented-out examples are not consumers either. A macro scan (`key is
    None`) drops a filter plugin, which can name a macro but never render it: `toposort.py`
    names `ingressroute.yml.j2`, and counting that refused 23 of 24 sampled ranges. Any other
    play-level hit refuses, a variable's too.
    """
    roles: set[str] = set()
    templates: set[str] = set()
    for path in hits:
        if path.endswith(".md") or _is_test_only_path(path):
            continue
        if key is None and filters.is_plugin(path):
            continue
        if path.startswith(INVENTORY):
            if path.split("/")[-1].startswith("_"):
                continue
            if key is None:
                raise CannotNarrow(
                    f"{subject} was scanned across the inventory with no key, so the "
                    f"mention in {path} cannot be told from a definition"
                )
            if _defines_only(key, path, ctx):
                continue
            raise CannotNarrow(
                f"{subject} is read by another value in {path}, whose own parsed value "
                "did not change"
            )
        if any(path.startswith(p) for p in PLAY_PREFIXES):
            raise CannotNarrow(f"{subject} is read by {path}, which every deploy runs")
        if path.startswith(SHARED_TEMPLATES):
            templates.add(path[len(SHARED_TEMPLATES) :])
            continue
        at = role_of(path)
        if at and at.plane == "k8s":
            roles.add(at.role)
    return Importers(roles, templates)


def template_importers(
    name: str, ref: str, cwd: Path, seen: set[str], explain=lambda _m: None
) -> set[str]:
    """Every role directory that imports or includes the shared template `name`.

    A macro imported by another macro reaches the second one's importers too, so the scan
    follows that edge rather than stopping at the first file. `seen` breaks a cycle and is
    also what keeps a self-reference from recursing. A play-level importer raises, in
    `sort_hits`.
    """
    ctx = Context(cwd, ref, set(), {}, explain)
    seen.add(name)
    found = narrow_templates.real_mentions(grep(ctx, name, word=False), name, ref, cwd)
    hits = sort_hits(found, f"the macro {name}", ctx)
    roles = narrow_templates.rendering_roles(name, hits.roles, ref, cwd, explain)
    for other in sorted(hits.templates - seen):
        roles |= template_importers(other, ref, cwd, seen, explain)
    return roles


def role_tags(roles: set[str], ctx: Context) -> set[str]:
    """The deploy tags a set of role directories selects under.

    A role with a `containers_list` entry IS a tag. A role without one is a shared role that
    other roles include by name, and `deploy.yml` runs it under each caller's tag — so it
    maps to its callers, and to theirs where a caller is itself shared. A shared role nobody
    calls can be applied by no tag at all, and refuses.
    """
    tags: set[str] = set()
    pending = set(roles)
    seen: set[str] = set()
    while pending:
        role = pending.pop()
        if role in seen:
            continue
        seen.add(role)
        if role in ctx.declared:
            tags.add(role)
            continue
        callers = ctx.callers.get(role) or set()
        if not callers:
            raise CannotNarrow(
                f"{role} has no containers_list entry and no caller, so no tag applies it"
            )
        pending |= callers
    return tags


def context_for(
    ref: str,
    cwd: Path | str,
    *,
    declared: set[str] | None = None,
    callers: dict[str, set[str]] | None = None,
    explain: Callable[[str], None] = lambda _m: None,
) -> Context:
    """A `Context` for reading `ref` from `cwd`, with the two derived fields filled in.

    Args:
        ref: the commit the rules read consumers at: the NEW end of a range.
        cwd: the checkout to read `ref` from.
        declared: the tags that exist; read at `ref` when omitted.
        callers: the k8s role-caller graph; walked from `cwd`'s working tree when omitted.
        explain: called with one derivation line per key, for stderr.

    Raises:
        subprocess.CalledProcessError: `ref` carries no host_vars, so `declared` cannot be
            read. A test driving a throwaway repo passes `declared` instead.
        CannotNarrow: a host_vars file at `ref` does not parse, or `role_callers` refuses a
            task file.
    """
    cwd = Path(cwd)
    if declared is None:
        try:
            declared = service_tags_at(ref, cwd)
        except yaml_fast.UnparsableYaml as exc:
            raise CannotNarrow(
                f"a host_vars file does not parse at {ref}: {exc}"
            ) from exc
    if callers is None:
        from lib.k8s_roles import role_callers

        # `cwd`, not the module-level REPO: the contract is "read this ref from this
        # checkout", and a graph walked from another tree answers for roles this checkout
        # may not even have.
        callers = role_callers(cwd)
    return Context(cwd, ref, declared, callers, explain)
