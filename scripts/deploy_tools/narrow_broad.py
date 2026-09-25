#!/usr/bin/env python3
"""Which service tags a deploy-plane change actually reaches, or a refusal to guess.

THE PROBLEM. `deploy_changes._BROAD_DEPLOY_PREFIXES` routes any change under
`ansible/inventory/` or `ansible/templates/` to an unscoped `ansible/deploy.yml`, which is
~20 minutes under the tree lock across every role. Measured from 2026-09-04: 14 such runs
were 12,966s of the 27,512s the lock was busy, and two of them failed on gates belonging to
services the change never touched, wrote `hold_sha`, and turned two other landings into
`deploy-failed (tick-held)`. Most of those ranges were one service's `containers_list` entry
or one variable two roles read.

WHAT THIS DERIVES. For each changed deploy-plane path, the services whose rendered output
can move because of it:

  - an inventory YAML file: the top-level keys whose value changed. `containers_list` maps
    an entry to its own tag(s), plus every role whose templates read the list itself (bare
    or through `hostvars[...]`, #2044); every other key is grepped for across the role trees
    and the shared templates, and a hit maps to that role's tag.
  - `ansible/templates/<f>.j2`: every role whose templates import or include `<f>`,
    following a macro that another macro imports.

ANY DOUBT IS A REFUSAL, and `deploy_handlers.handle_broad` turns a refusal back into today's
full run. A missed consumer is a service left silently stale until something unrelated
redeploys it; a full run is only slow.

`lib.narrow_git` holds the primitives this shares with `narrow_setup`: `CannotNarrow`,
`show_at` and the YAML mapping parse, one copy each since #2419.

`narrow` agrees with `deploy_tags.py changed` wherever both answer: the non-broad half of a
range goes through the same `services_from_changed_paths` mapper. Where `changed` prints a
tag list PLUS a note about a shared role a human must still apply, `narrow` refuses instead —
the tick has no human to read the note.

`Release Staleness Drift` is the second consumer (`probe_lib/releases.py`, #1993). It asks
the per-path question `broad_path_tags` answers, over `CENSUS_PREFIXES`, for the range from
a service's release record to `origin/master` — so a merged inventory or macro change that
reached a service's render is named stale until that service is re-stamped. It does NOT go
through `narrow`: the fleet-coverage ceiling at the end of `narrow` is lock-time policy (a
list covering most of the fleet saves none of the twenty minutes), and for a census "most of
the fleet" is the answer, not a refusal.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_tags_narrow.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, NamedTuple

from deploy_tools import narrow_containers, narrow_paths
from deploy_tools.exit_codes import DEPLOY_BROAD, DEPLOY_OK
from lib.git import git, git_stdout
from lib.narrow_git import CannotNarrow, changed_mapping_keys, mapping_at, show_at
from lib.render_guard import service_tags_at
from lib.repo_paths import GITOPS_DEPLOY_FILES, REPO

# The deployer's own `files/` — `deploy_logic` is imported from there, the same reach across
# the role boundary `deploy_tags.py` makes and for the same reason: one mapper, not two. Its
# own insert rather than deploy_tags', so this module resolves however it is reached. The
# IMPORTS stay inside the functions, because only the path entry is free.
_sys.path.insert(0, str(GITOPS_DEPLOY_FILES))

# Paths whose content every play reads, so no `--tags` value scopes a change to them: the
# play itself and the three task directories it imports, the toposort that orders the whole
# run, the shared Docker deploy path, and the config every ansible-playbook run reads.
PLAY_PREFIXES = (
    "ansible/deploy.yml",
    "ansible/tasks/",
    "ansible/pre_tasks/",
    "ansible/post_tasks/",
    "ansible/filter_plugins/",
    "ansible/roles/containers/common/",
    "ansible.cfg",
)
# The trees a variable or a macro can be consumed from and still map to a deploy tag.
# `ansible/roles/setup/` is absent on purpose: the setup plane has its own arm in
# `handle_broad`, and `ansible/deploy.yml` renders nothing for it.
ROLE_TREES = ("ansible/roles/k8s", "ansible/roles/containers")
SHARED_TEMPLATES = "ansible/templates/"
INVENTORY = "ansible/inventory/"
# The two broad-deploy trees a per-path rule exists for. `PLAY_PREFIXES` are deliberately
# absent: `broad_path_tags` refuses them outright, and a census that counted them would mark
# every service stale for a change to how a deploy RUNS — the shape #1672 paid for.
CENSUS_PREFIXES = (INVENTORY, SHARED_TEMPLATES)

_ROLE_PATH = re.compile(r"^ansible/roles/(?:k8s|containers)/([^/]+)/")
# Python under the play's own tree cannot import a Jinja macro, so a macro NAME found there is
# a string, not a consumer: `filter_plugins/toposort.py` carries `ingressroute.yml.j2` as the
# marker it greps role templates for. Treating that hit as consumption refused every change to
# that macro — 23 of 24 sampled deploy-plane ranges over the 600 commits to 2026-09-18 — and
# since #1993 the same refusal marks every service on a record stale and pages (#2001). A
# variable is different: a filter plugin can read one, so the variable scan keeps refusing.
_FILTER_PLUGINS = "ansible/filter_plugins/"
# Directories under the role trees that are not services, as `land_tags._NOT_SERVICES` has
# them: `common` is the shared Docker deploy path and `archive` holds retired roles.
_NOT_SERVICES = frozenset({"common", "archive"})


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


def _grep(
    ctx: Context, pattern: str, *, word: bool, inventory: bool = False
) -> list[str]:
    """Every tracked path at `ctx.ref` whose content contains `pattern`, as a fixed string.

    `inventory` adds the inventory tree, which a variable scan needs and a macro scan does
    not. A variable can be consumed by another variable — `foo: "{{ bar }}"` — and the key
    diff cannot see that, because `foo`'s own parsed value did not change when `bar` did.
    Every key also hits its own definition line, so `_sort_hits` tells the two apart.
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
    counts, because telling a real `#` from one inside a quoted value needs a parse — doubt
    runs the whole play.

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


def _sort_hits(
    hits: list[str], subject: str, ctx: Context, key: str | None = None
) -> Importers:
    """Split grep hits into roles and shared templates; a play-level hit refuses.

    A `.md` is prose no playbook applies, and a role's own `tests/` reaches no host — both
    are dropped for the same reasons `land_tags.role_for` and `is_role_test_path` drop them.
    An inventory hit that is not `key`'s own definition refuses: see `_defines_only`. A
    `_`-prefixed inventory file is exempt on both sides — `_inventory_tags` skips it as a
    file no host loads, so its commented-out examples are not consumers either. A macro
    scan (`key is None`) drops a `.py` under `_FILTER_PLUGINS`, which can name a macro but
    never render it; a play-level hit anywhere else, and every play-level hit for a
    variable, still refuses.
    """
    roles: set[str] = set()
    templates: set[str] = set()
    for path in hits:
        if path.endswith(".md"):
            continue
        if key is None and path.startswith(_FILTER_PLUGINS) and path.endswith(".py"):
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
        m = _ROLE_PATH.match(path)
        if m and m.group(1) not in _NOT_SERVICES and path.split("/")[4:5] != ["tests"]:
            roles.add(m.group(1))
    return Importers(roles, templates)


def template_importers(
    name: str, ref: str, cwd: Path, seen: set[str], explain=lambda _m: None
) -> set[str]:
    """Every role directory that imports or includes the shared template `name`.

    A macro imported by another macro reaches the second one's importers too, so the scan
    follows that edge rather than stopping at the first file. `seen` breaks a cycle and is
    also what keeps a self-reference from recursing. A play-level importer raises, in
    `_sort_hits`.
    """
    ctx = Context(cwd, ref, set(), {}, explain)
    seen.add(name)
    hits = _sort_hits(_grep(ctx, name, word=False), f"the macro {name}", ctx)
    roles = set(hits.roles)
    for other in sorted(hits.templates - seen):
        roles |= template_importers(other, ref, cwd, seen, explain)
    return roles


def _role_tags(roles: set[str], ctx: Context) -> set[str]:
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


def _containers_list_tags(
    before: dict, after: dict, ctx: Context, path: str
) -> set[str]:
    """The tags a `containers_list` change reaches: its entries' own, and the list's readers.

    The readers are counted for every host's list at once: which host a bare read means
    depends on the play that renders it, and one redundant redeploy is the safe direction.
    `narrow_containers.entry_change_tags` carries why a removed entry maps to nothing.
    """
    own = narrow_containers.entry_change_tags(before, after, ctx.explain, path)
    return own | _list_reader_tags(ctx, path)


def _list_reader_tags(ctx: Context, path: str) -> set[str]:
    """The tags of every role whose templates render `containers_list` as a whole.

    A grep over the role trees and the shared templates, keeping a hit only where the name
    sits inside Jinja code (`narrow_containers.reader_paths` says which hits are prose and
    which are the play's own iteration, and drops both rather than refusing).
    """
    hits = narrow_containers.reader_paths(
        _grep(ctx, "containers_list", word=True),
        PLAY_PREFIXES,
        lambda hit: show_at(ctx.ref, hit, ctx.cwd),
    )
    found = _sort_hits(hits, "containers_list", ctx, "containers_list")
    roles = set(found.roles)
    for name in sorted(found.templates):
        roles |= template_importers(name, ctx.ref, ctx.cwd, {name}, ctx.explain)
    tags = _role_tags(roles, ctx)
    ctx.explain(
        f"narrow: containers_list readers -> {','.join(sorted(tags)) or '(nothing)'}"
        f" via {path}"
    )
    return tags


def _key_tags(key: str, ctx: Context, path: str) -> set[str]:
    """The tags of every role that reads the inventory variable `key`.

    A key nothing reads maps to nothing: an unused variable renders into no file. A key the
    play itself reads, and a key another inventory value reads, both refuse inside
    `_sort_hits`.
    """
    hits = _sort_hits(
        _grep(ctx, key, word=True, inventory=True), f"the variable {key}", ctx, key
    )
    roles = set(hits.roles)
    for name in sorted(hits.templates):
        try:
            roles |= template_importers(name, ctx.ref, ctx.cwd, {name}, ctx.explain)
        except CannotNarrow as exc:
            # A macro this key reaches refuses under its own name, and a macro that macro
            # imports refuses under the NESTED name. Neither says which key reached it.
            ctx.explain(f"narrow: {key} -> macro {name} via {path}")
            raise CannotNarrow(f"the variable {key} cannot be narrowed: {exc}") from exc
    try:
        tags = _role_tags(roles, ctx)
    except CannotNarrow as exc:
        # The journal needs the derivation line even on the refusal: without it the reason
        # names a role and nothing says which key reached it.
        ctx.explain(f"narrow: {key} -> roles {','.join(sorted(roles))} via {path}")
        raise CannotNarrow(f"the variable {key} reaches a role where {exc}") from exc
    ctx.explain(f"narrow: {key} -> {','.join(sorted(tags)) or '(nothing)'} via {path}")
    return tags


def _inventory_tags(
    path: str, before: str | None, after: str, ctx: Context
) -> set[str]:
    """The tags one changed inventory file reaches, key by key.

    `hosts.ini` and anything else that is not YAML refuses: the host list and its groups
    decide which machines a play visits at all, which no `--tags` value narrows. A
    `_`-prefixed file is loaded by no host (`_example.yml`), so it maps to nothing.
    """
    name = path.split("/")[-1]
    if not name.endswith((".yml", ".yaml")):
        raise CannotNarrow(f"{path} is not an inventory YAML file")
    if name.startswith("_"):
        return set()
    # An inventory file the range ADDS has no before-state, and every key in it is new.
    old = mapping_at(before or "", path)
    new = mapping_at(after, path)
    tags: set[str] = set()
    for key in sorted(changed_mapping_keys(old, new, path)):
        if key == "containers_list":
            tags |= _containers_list_tags(old, new, ctx, path)
        else:
            tags |= _key_tags(key, ctx, path)
    return tags


def broad_path_tags(path: str, old_ref: str, ctx: Context) -> set[str]:
    """The tags one changed broad-deploy path reaches between `old_ref` and `ctx.ref`.

    The unit both consumers share: `narrow` calls it per path of the tick's range, and
    `releases.compute_stale` per path of a record's range. Raises `CannotNarrow` where no
    rule can say — and the two consumers read that refusal differently. The tick runs the
    whole play, which re-stamps every service; the census marks every service sharing the
    record stale, which is the set that full run would have re-stamped. The two agree by
    construction, so a refusal never leaves a service the tick applied reading stale.
    """
    if narrow_paths.is_prose(path):  # a doc no playbook applies (#2448)
        return set()
    if any(path.startswith(p) for p in PLAY_PREFIXES):
        raise CannotNarrow(f"{path} is read by every deploy")
    after = show_at(ctx.ref, path, ctx.cwd)
    if path.startswith(SHARED_TEMPLATES):
        name = path[len(SHARED_TEMPLATES) :]
        roles: set[str] = set()
        try:
            roles = template_importers(name, ctx.ref, ctx.cwd, {name}, ctx.explain)
            if after is None:
                # A DELETED macro nothing imports reaches no render, and that is provable
                # rather than a guess: an importer left behind would fail to render, and CI
                # renders every template — so every importer it had at `old_ref` changed or
                # was deleted in the same range and narrows on its own. Commit `8d825a872`
                # deleted the unimported `ansible/templates/traefik.yml.j2` and the blanket
                # refusal bought the whole ~20-minute `deploy.yml` play for it (#2440). An
                # importer that survived, or a scan that cannot run, still refuses.
                if roles:
                    raise CannotNarrow(
                        f"it is deleted but {','.join(sorted(roles))} imports it"
                    )
                ctx.explain(f"narrow: {name} deleted -> (nothing) via {path}")
                return set()
            tags = _role_tags(roles, ctx)
        except CannotNarrow as exc:
            # The same treatment `_key_tags` gives its own refusal, for the same reason:
            # both inner raises name a role or a nested macro, so without this the journal
            # never says which changed template reached it.
            ctx.explain(f"narrow: {name} -> roles {','.join(sorted(roles))} via {path}")
            raise CannotNarrow(f"the macro {name} cannot be narrowed: {exc}") from exc
        ctx.explain(
            f"narrow: {name} -> {','.join(sorted(tags)) or '(nothing)'} via {path}"
        )
        return tags
    if after is None:
        raise CannotNarrow(f"{path} was deleted, so nothing can be read from it")
    if path.startswith(INVENTORY):
        return _inventory_tags(path, show_at(old_ref, path, ctx.cwd), after, ctx)
    raise CannotNarrow(f"{path} is a broad path no narrowing rule reads")


def _changed_half(paths: list[str], ctx: Context) -> set[str]:
    """The tags the NON-broad paths in the range reach — the mapper `changed` already uses.

    A setup-plane path contributes nothing here: `deploy_narrow.plan` gives the setup half
    its own `initial_setup.yml` plan ahead of this one, so the two planes are applied side by
    side. It refused until 2026-09-18 (#2046), when the planner was an if/else that dropped
    the deploy half of a mixed range — refusing here only made the drop a full run nobody
    ran. A bring-up playbook still refuses: the tick parks on those before any plan exists,
    and a hand `deploy_tags.py narrow` over such a range must not read as applyable.
    `cs.tasks`/`cs.meta`/`cs.secrets` refuse for the opposite reason: `changed` reports them
    as work a human deploys by hand, and the full run this replaces DOES apply them. A
    rotated secret reaches a service only when that service renders again, so narrowing a
    range that carries one would leave every service outside the tag list on the old value.
    """
    from deploy_logic import expand_build_couplings, services_from_changed_paths

    cs = services_from_changed_paths(paths)
    if cs.broad_manual:
        raise CannotNarrow("the range also changes a bring-up playbook")
    if cs.secrets:
        raise CannotNarrow(
            "the range rotates a secret, which reaches a service only on its next render"
        )
    if cs.tasks or cs.meta:
        raise CannotNarrow(
            f"structural change in {sorted(cs.tasks | cs.meta)}, which no tag captures"
        )
    return _role_tags(expand_build_couplings(cs.k8s) | cs.services, ctx)


def narrow(
    old_ref: str,
    new_ref: str,
    *,
    cwd: Path | str = REPO,
    declared: set[str] | None = None,
    callers: dict[str, set[str]] | None = None,
    explain: Callable[[str], None] = lambda _m: None,
) -> set[str]:
    """Every tag a deploy-plane range reaches. Raises `CannotNarrow` when it cannot say.

    Args:
        old_ref: the commit the checkout is on.
        new_ref: the commit it is about to fast-forward to.
        cwd: the checkout to read both refs from.
        declared: the tags that exist; read at `new_ref` when omitted.
        callers: the k8s role-caller graph; walked from `cwd`'s working tree when omitted.
        explain: called with one derivation line per key, for stderr.

    Returns:
        The tags to pass to `ansible/deploy.yml --tags`. An empty set means the range moves
        no rendered output at all, and the fast-forward IS the whole apply.
    """
    cwd = Path(cwd)
    ctx = context_for(new_ref, cwd, declared=declared, callers=callers, explain=explain)
    broad_prefixes = _broad_deploy_prefixes()
    paths = [
        p
        for p in git_stdout(
            "diff", "--name-only", f"{old_ref}..{new_ref}", cwd=cwd
        ).splitlines()
        if p
    ]
    broad = [p for p in paths if any(p.startswith(x) for x in broad_prefixes)]
    tags = _changed_half([p for p in paths if p not in set(broad)], ctx)
    for path in broad:
        tags |= broad_path_tags(path, old_ref, ctx)
    # A tag list covering most of the fleet saves none of the twenty minutes this exists to
    # save, and adds a way to miss something the whole play would have done. `manifests` is
    # the shape that reaches it: 54 roles include it, so any variable it reads fans out to
    # every caller.
    if len(tags) * 2 > len(ctx.declared):
        raise CannotNarrow(
            f"{len(tags)} of {len(ctx.declared)} services — most of the fleet, which is not a "
            "narrowing"
        )
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
        ref: the commit the rules read consumers at — the NEW end of a range.
        cwd: the checkout to read it from.
        declared: the tags that exist; read at `ref` when omitted.
        callers: the k8s role-caller graph; walked from `cwd`'s working tree when omitted.
        explain: called with one derivation line per key.

    Raises:
        subprocess.CalledProcessError: `ref` carries no host_vars, so `declared` cannot be
            read. A test driving a throwaway repo passes `declared` instead.
    """
    cwd = Path(cwd)
    if declared is None:
        declared = service_tags_at(ref, cwd)
    if callers is None:
        from lib.k8s_roles import role_callers

        # `cwd`, not the module-level REPO: the contract is "read this ref from this
        # checkout", and a graph walked from another tree answers for roles this checkout
        # may not even have.
        callers = role_callers(cwd)
    return Context(cwd, ref, declared, callers, explain)


def _broad_deploy_prefixes() -> tuple[str, ...]:
    """`deploy_changes._BROAD_DEPLOY_PREFIXES`, through the deployer's own index."""
    from deploy_logic import _BROAD_DEPLOY_PREFIXES

    return _BROAD_DEPLOY_PREFIXES


def narrow_cmd(
    old_ref: str,
    new_ref: str,
    *,
    cwd: Path | str = REPO,
    declared: set[str] | None = None,
    callers: dict[str, set[str]] | None = None,
) -> int:
    """Print the narrowed tag list on stdout; the derivation and any refusal on stderr.

    Nothing but the tags reaches stdout, so `handle_broad` can read it straight. An empty
    stdout with exit 0 is the real answer "this range reaches no rendered output".
    """

    def explain(message: str) -> None:
        print(message, file=sys.stderr)

    try:
        tags = narrow(
            old_ref,
            new_ref,
            cwd=cwd,
            declared=declared,
            callers=callers,
            explain=explain,
        )
    except CannotNarrow as exc:
        print(f"narrow: cannot narrow ({exc}) — full deploy.yml", file=sys.stderr)
        return DEPLOY_BROAD
    except subprocess.CalledProcessError as exc:
        # `service_tags_at` and the diff read refs the caller handed us; an unreadable one
        # is a refusal like any other, not a traceback the deployer logs as a crash.
        print(
            f"narrow: cannot narrow (git could not read the range: {exc}) — full "
            "deploy.yml",
            file=sys.stderr,
        )
        return DEPLOY_BROAD
    if tags:
        print(",".join(sorted(tags)))
    else:
        print("narrow: the range reaches no rendered output", file=sys.stderr)
    return DEPLOY_OK
