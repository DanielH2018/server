"""Derive deploy tags from a merged PR's own file list.

WHY NOT A SHA RANGE. A commit range after a merge covers every other session's merged work
too. Deploying somebody else's half-finished landing is not this session's to do. A PR's file
list is exactly this session's scope.

WHY A ROLE NAME IS NOT AUTOMATICALLY A TAG. Only a role with a `containers_list` entry has
one. Several roles under ansible/roles/k8s/ have no entry because other roles include them by
literal name, and a landing ends `deploy-failed` on a tag no k8s entry declares. Those roles
come out of the tags.

WHY THE COUNT ASSERTION. `gh pr view --json files` paginates at 100. A 137-file PR returns
100 entries with no error and no marker, so the derived tag list is a silent subset of what
merged -- and every downstream check reads green over it. When the returned count disagrees
with the PR's own `changedFiles`, this reports `fallback` and the caller widens to the diff
since `--since`: wider than the truth is recoverable, narrower is not.

Run: uv run pytest scripts/deploy_tools/tests/test_land_tags.py
"""

import re
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # scripts/

from lib.git import git, git_stdout
from lib.render_guard import (  # noqa: F401  (`service_tags_at` is read through this module)
    containers_entries,
    entry_tags,
    host_files,
    service_tags_at,
)
from lib.repo_paths import REPO
from lib.role_paths import SECRETS_FILE, tag_for

# The remediation for a rotated secret. Flat text rather than derived from the file list,
# because the consuming role is not knowable from here: a secret's value lives in no role's
# template, so the changed-path matching every other rule uses has nothing to match on.
#
# Named for the rotation it describes, not for the file that triggers it. CodeQL classifies a
# constant whose name reads as `SECRET` as sensitive data, so `print(plane_note(...))` was
# reported as py/clear-text-logging-sensitive-data every time a refactor moved that line. This
# constant is operator prose and holds no credential.
_ROTATION_NOTE = (
    "`ansible/vars/secrets.yml` changed, and a secret's VALUE lives in no role's template — so "
    "a rotation **maps to no deploy tag by construction**, and a consumer keeps rendering the "
    "OLD value until its own role renders again. Resolve them: `uv run python "
    "scripts/secrets_mgmt/secret_rotation.py consumers <secret>` greps the tree for who now "
    "holds a stale copy and prints the exact repair command per plane; run each printed "
    "command."
)

# Where a k8s consumer of a changed key gets the new value, appended to `_ROTATION_NOTE`. A
# flux-owned role reads the value through `homelab-cluster-vars`, which only
# `cluster_vars.py write` regenerates, and `promote` then applies it.
_K8S_READERS_NOTE = (
    "A k8s consumer reads a changed key through `homelab-cluster-vars`: regenerate it with "
    "`cluster_vars.py write` (operator-only), and `promote` applies it with the next bundle."
)


def declared_tags() -> set[str]:
    """Every name that selects a service, read from containers_list."""
    return {
        tag
        for path in host_files()
        for entry in containers_entries(path)
        for tag in entry_tags(entry)
    }


def plane_note(files) -> str:
    """What this PR still needs a HUMAN to apply, or "" if nothing.

    A rotated secret is the one shape left. A secret's value lives in no role's template, so
    `ansible/vars/secrets.yml` derives zero tags however many roles consume it: every consumer
    keeps rendering the old value, and the landing would report `nothing-to-deploy`.
    """
    # DECIDED: fire on ANY change to secrets.yml, and never try to name which keys moved.
    # Naming them means decrypting both revisions, and no plaintext may reach a terminal, a
    # transcript or a log. Over-firing on a key that needed no redeploy costs one exit code;
    # under-firing leaves a rotated credential stale in a consumer this file list cannot show.
    if SECRETS_FILE in files:
        return f"{_ROTATION_NOTE} {_K8S_READERS_NOTE}"
    return ""


class DeriveSource(StrEnum):
    """Where a tag list came from, and therefore whether it can be trusted whole."""

    PR = "pr"
    FALLBACK = "fallback"


class Derivation(NamedTuple):
    """What `derive` answers: the tags, and whether the file list could be trusted."""

    tags: list[str]
    source: DeriveSource


def derive(
    files, changed_files: int, declared: set[str] | None = None, quiet=()
) -> Derivation:
    """(sorted tags, 'pr'|'fallback').

    'fallback' means the file list could not be trusted and the caller must widen to a SHA
    range. The tag list returned alongside it is empty on purpose: a partial list is worse
    than none, because it looks like an answer.

    `quiet` is `quiet_paths`'s set: a comment-only k8s `defaults/` edit renders the same
    manifests (#4460), so it deploys nothing.
    """
    files = list(files)
    if len(files) != changed_files:
        return Derivation([], DeriveSource.FALLBACK)
    declared = declared_tags() if declared is None else declared
    quiet = frozenset(quiet)
    tags = {t for p in files if p not in quiet and (t := tag_for(p, declared))}
    return Derivation(sorted(tags), DeriveSource.PR)


def doc_paths(paths) -> set[str]:
    """The paths in `paths` that are documentation, which nothing renders: a `.md` only."""
    return {p for p in paths if p.endswith(".md")}


# A k8s role's `defaults/` file. Ansible reads it only through its values, so an edit that
# moves no content line renders the same manifests. `manifests` is left out: it is the renderer
# every service includes.
_K8S_DEFAULTS_FILE = re.compile(
    r"^ansible/roles/k8s/(?!manifests/)[^/]+/defaults/[^/]+\.ya?ml$"
)
# `key: |` / `key: >-` and their chomping/indent modifiers, with or without a trailing comment.
# A bare list item `- |` opens one too; missing it read the scalar's `#` lines as comments.
_BLOCK_SCALAR_HEAD = re.compile(r"(?::|^\s*-)\s*[|>][-+0-9]*\s*(?:#.*)?$")


def _content_lines(text: str) -> list[str]:
    """The lines of a YAML file that carry meaning.

    Full-line comments and blank lines are dropped, except inside a block scalar, where a
    line starting with `#` is part of the string and a blank line is a newline in it. The
    scalar runs from its `key: |` line until the next non-blank line indented no deeper
    than that key. A trailing comment on a content line is kept, so such a line reads as
    changed.
    """
    kept: list[str] = []
    scalar_indent: int | None = None
    for line in text.splitlines():
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if scalar_indent is not None:
            if stripped and indent <= scalar_indent:
                scalar_indent = None
            else:
                kept.append(line.rstrip())
                continue
        if not stripped or stripped.startswith("#"):
            continue
        kept.append(line.rstrip())
        if _BLOCK_SCALAR_HEAD.search(line):
            scalar_indent = indent
    return kept


def _git_show(ref: str, path: str) -> str:
    """`git show <ref>:<path>`, unstripped, because the file's own trailing newline is content."""
    return git("show", f"{ref}:{path}", cwd=REPO).stdout


def comment_only_k8s_defaults(paths, old: str, new: str, show=_git_show) -> set[str]:
    """The k8s role `defaults/` files whose change between two refs is comments only.

    A path that is added, deleted or unreadable on either side is left out: the fail-safe
    direction is to keep the change, never to skip past one this did not read.
    """
    quiet: set[str] = set()
    for p in paths:
        if not _K8S_DEFAULTS_FILE.match(p):
            continue
        try:
            before, after = show(old, p), show(new, p)
        except RuntimeError, subprocess.CalledProcessError, OSError:
            continue
        if _content_lines(before) == _content_lines(after):
            quiet.add(p)
    return quiet


def quiet_paths(paths: list[str], range_: str) -> set[str]:
    """The paths in `paths` a landing deploys nothing for: docs, and comment-only defaults.

    Empty of comment-only paths when no range was given, or when the range is malformed, or
    when git cannot read a side of it -- every one of those keeps the path loud, which is the
    direction a wrong answer here must fall. The docs half survives all three, because it
    asks nothing of the range.

    A RANGE NARROWER THAN THE FILE LIST is the same failure wearing a valid range: a path
    changed OUTSIDE the range reads as identical on both sides, so it would come back quiet.
    So every path asked about must appear in the range's own diff, or nothing is quiet --
    the same shape as `derive`'s count assertion, and for the same reason.
    """
    docs = doc_paths(paths)
    old, sep, new = range_.partition("..")
    if not (sep and old and new):
        return docs
    try:
        covered = set(
            git_stdout("diff", "--name-only", f"{old}..{new}", cwd=REPO).splitlines()
        )
        if not set(paths) <= covered:
            return docs
        return docs | comment_only_k8s_defaults(paths, old, new)
    except subprocess.CalledProcessError, OSError:
        return docs
