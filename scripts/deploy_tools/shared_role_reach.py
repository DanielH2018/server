#!/usr/bin/env python3
"""Which changed SHARED k8s roles move no rendered manifest, so no hand has to apply them.

THE PROBLEM. A shared role — `manifests`, `volume-claim`, `volume-revert` — has no
`containers_list` entry, so no `--tags` value applies it and `land_tags.plane_note` reports it
as work a human still owes: a full `ansible/deploy.yml`, about 20 minutes. That is the right
answer when the change moves the bytes a service has live. It is the wrong answer when the
change is a default only a task reads while a deploy runs. PR #2460 changed
`manifests_rollout_timeout_default` and the task passing it as `--timeout`; no live object
carries that number, and the full run asked for would have changed nothing (#2462).

WHAT THIS DERIVES. For one shared role's changed paths, whether every one of them is
deploy-time only:

  - `templates/` or `files/`: never. Those bytes are applied or shipped.
  - `defaults/main.yml`, `vars/<f>.yml`: the top-level keys whose value changed, then
    whether any template anywhere mentions one. A mentioned key can render into a manifest.
  - `tasks/`, `handlers/`, `meta/`: only when every line the diff changed mentions one of
    those quiet keys. A task edit that reads the changed default is that default's own
    consumer; a task edit that does anything else can move the bytes it renders.

DECIDED: per KEY, not per role and not per subdirectory. `releases._supplies_manifest_bytes`
names `manifests` as byte-supplying outright and `releases._DEPLOY_TIME_SUBDIRS` leaves
`defaults/` out, both deliberately — `volume-claim/defaults/main.yml` holds
`volume_claim_size`, which its own `pvc.yaml.j2` renders, and `manifests/tasks/` IS the render
and apply logic (#947, #1636, #1672). Neither prior is overturned here: a key a template
mentions stays loud, and a `tasks/` change that is not a changed key's consumer stays loud.
This only refines them one level finer, at the key.

ANY DOUBT KEEPS THE ROLE LOUD. An unreadable range, a `defaults/main.yml` that does not
parse, a grep that fails, a path in a directory no rule names — every one of them returns the
role to `plane_note`, which asks for the full deploy. Reporting work that is already done
costs one hand check; the other direction leaves a service silently stale.

Run: uv run pytest scripts/deploy_tools/tests/test_shared_role_reach.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import re
from pathlib import Path

from deploy_tools import land_tags
from lib.git import git
from narrow_setup_index import CannotNarrow
from narrow_setup import changed_keys

# The trees a template can live in. `ansible/templates/` is the shared macro directory; under
# a role, only `templates/` renders. `files/` is shipped verbatim, so a key cannot reach it by
# name and it needs no grep — the path rule below refuses it outright.
_TEMPLATE_TREES = ("ansible/roles", "ansible/templates")
# Subdirectories of a role whose content decides how a deploy RUNS. The same three
# `releases._DEPLOY_TIME_SUBDIRS` names, and `defaults`/`vars` are deliberately absent here
# too: they are handled by the key scan, which is the whole point of this module.
_DEPLOY_TIME_SUBDIRS = frozenset({"tasks", "handlers", "meta"})
_VARS_SUBDIRS = frozenset({"defaults", "vars"})


def _role_prefixes(role: str) -> tuple[str, ...]:
    return (
        f"ansible/roles/k8s/{role}/",
        f"ansible/roles/containers/{role}/",
    )


def _subdir(path: str, role: str) -> str:
    """The role subdirectory `path` sits in (`tasks`, `defaults`, ...), or ''."""
    for prefix in _role_prefixes(role):
        if path.startswith(prefix):
            rest = path[len(prefix) :].split("/")
            return rest[0] if len(rest) > 1 else ""
    return ""


def _mentioned_by_a_template(key: str, ref: str, repo: Path) -> bool:
    """Whether any template at `ref` mentions `key`.

    A MENTION, not a render: the three `registry` templates and `ansible/templates/
    ingressroute.yml.j2` name `manifests_prune` in comments only, and this counts all four.
    Telling a comment from an interpolation needs a parse of Jinja inside YAML, and the cost
    of counting a comment is that one key stays loud — the safe direction.
    """
    r = git(
        "grep",
        "-l",
        "-w",
        "-F",
        "-e",
        key,
        ref,
        "--",
        *_TEMPLATE_TREES,
        cwd=repo,
        check=False,
    )
    if r.returncode > 1:
        raise CannotNarrow(f"`git grep {key}` failed: {r.stderr.strip()}")
    prefix = f"{ref}:"
    hits = [line[len(prefix) :] for line in r.stdout.splitlines() if line]
    return any(
        hit.startswith("ansible/templates/") or "/templates/" in hit for hit in hits
    )


def _changed_lines(path: str, old: str, new: str, repo: Path) -> list[str]:
    """The added and removed content lines of `path` between two refs.

    `-U0` so no unchanged context is read as a change. The `+++`/`---` headers are dropped by
    length, which is why the slice is taken before the sigil test.
    """
    r = git("diff", "-U0", f"{old}..{new}", "--", path, cwd=repo, check=False)
    if r.returncode != 0:
        raise CannotNarrow(f"`git diff` of {path} failed: {r.stderr.strip()}")
    out = []
    for line in r.stdout.splitlines():
        if line.startswith(("+++", "---", "@@", "diff ", "index ")):
            continue
        if line[:1] in ("+", "-"):
            out.append(line[1:])
    return out


def _tasks_read_only_these_keys(
    path: str, keys: set[str], old: str, new: str, repo: Path
) -> bool:
    """Whether every line the diff of `path` changed mentions one of `keys`.

    A blank line and a whole-line comment count as quiet: neither is something Ansible
    applies, and `manifests/tasks/main.yml` carries prose beside the task it changed. A
    trailing comment on a real line is not split out — doubt keeps the role loud.
    """
    if not keys:
        return False
    for line in _changed_lines(path, old, new, repo):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Word-bounded, the way `narrow_broad._grep` bounds its own key scan: without it a
        # line reading `foo_bar` counts as a consumer of an unrelated `foo`, which is the
        # direction this module must never fall.
        if not any(
            re.search(rf"(?<!\w){re.escape(key)}(?!\w)", stripped) for key in keys
        ):
            return False
    return True


def deploy_time_only(role: str, files, pr_range: str, repo) -> bool:
    """Whether every changed path under shared role `role` moves no rendered manifest.

    Args:
        role: the shared role's directory name, as `land_tags.shared_roles` returns it.
        files: the PR's changed paths, the whole list.
        pr_range: `<old>..<new>` bounding this PR's own change.
        repo: the checkout holding both ends of the range.

    Returns:
        True only when every changed path under the role is a vars file whose changed keys no
        template mentions, or a `tasks`/`handlers`/`meta` file whose diff changed nothing but
        lines reading one of those keys. False for every other path, and for every failure.
    """
    if ".." not in pr_range:
        return False
    old, new = pr_range.split("..", 1)
    repo = Path(repo)
    paths = [p for p in files if _subdir(p, role)]
    if not paths:
        return False
    vars_paths = [p for p in paths if _subdir(p, role) in _VARS_SUBDIRS]
    other = [p for p in paths if p not in set(vars_paths)]
    if any(_subdir(p, role) not in _DEPLOY_TIME_SUBDIRS for p in other):
        return False
    try:
        keys: set[str] = set()
        for path in vars_paths:
            keys |= changed_keys(path, old, new, str(repo))
        if any(_mentioned_by_a_template(key, new, repo) for key in sorted(keys)):
            return False
        return all(_tasks_read_only_these_keys(p, keys, old, new, repo) for p in other)
    except CannotNarrow:
        return False
    except OSError:
        return False


def deploy_time_only_roles(
    files, pr_range: str, repo, declared: set[str] | None = None
) -> frozenset[str]:
    """The shared roles this PR changes that move no rendered manifest.

    Empty whenever the range is unknown, which keeps every shared role loud.
    """
    files = list(files)
    return frozenset(
        role
        for role in land_tags.shared_roles(files, declared)
        if deploy_time_only(role, files, pr_range, repo)
    )


def paths_a_hand_must_apply(
    files, pr_range: str, repo, declared: set[str] | None = None
) -> list[str]:
    """`files` minus every path under a shared role that moves no rendered manifest.

    The list `land_tags.plane_note` is built from. Subtracting the PATHS rather than passing
    the roles is what keeps `plane_note` a decision over a path list, the way it is for
    `quiet`: a shared role has no `containers_list` entry by construction, so dropping its
    paths removes it from `shared_roles` and changes nothing else the note reads —
    `derived_tags` maps them to no tag and `services_from_changed_paths` puts them in neither
    the broad nor the setup half.

    Returns `files` unchanged on every failure inside, which keeps every shared role in the
    note.
    """
    files = list(files)
    roles = deploy_time_only_roles(files, pr_range, repo, declared)
    if not roles:
        return files
    return [p for p in files if not any(_subdir(p, role) for role in roles)]
