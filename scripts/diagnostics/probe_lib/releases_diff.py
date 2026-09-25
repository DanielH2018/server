"""The one staleness question a path cannot answer: did this `tasks/` diff move any bytes?

`releases._is_real_change` is a path predicate, and that is enough for every rule but one. A
k8s SERVICE role's own `tasks/main.yml` names its `manifests_files`, and passes
`volume_claim_size` and `volume_claim_storage_class` to `k8s/volume-claim`, whose
`pvc.yaml.j2` reads both -- so a change there usually does move the applied bytes and must
count. Usually is not always. `7fd4189cb` added `check_mode: false` and `when: not
ansible_check_mode` guards across nineteen roles; every one renders byte-identical manifests
on a real deploy, and tdarr's marked it stale with nothing able to clear it but a hand-run
no-op of a deploy (#2416).

So this module reads the diff rather than the path, for that one class. A changed line a real
(non-check) run cannot see moves no bytes: `check_mode: false` is a no-op outside `--check`,
and a comment or a blank line is never evaluated. Anything else keeps the file -- a line this
module does not recognise, a diff git cannot produce, a rename carrying no changed line at all
-- because a drift monitor that guesses wrong has to guess toward visible.

`check_mode: true` is deliberately not inert. That one DOES change a real run: it puts a task
into check mode that would otherwise have made its change.

WHY A `when:` IS COMPARED RATHER THAN MATCHED (#2436). `7fd4189cb` spelled its gate three
ways. tdarr, headlamp and prowlarr each got `when: not ansible_check_mode` on its own line, and
a line-shape match settled those. authelia instead turned a scalar `when: <expr>` into the list
form and added `- not ansible_check_mode` as a second conjunct, so its diff carries a removed
`when: <expr>`, an added bare `when:` and two added list items -- no line shape covers that, and
authelia kept reading stale for a change that renders identical manifests.

The rule this module applies instead is still a shape test, not an Ansible interpreter. It
collects the CONDITIONS a hunk changed, from a `when:` scalar and from the list items under a
bare `when:` key, and calls the hunk inert when the added conditions equal the removed ones
apart from `not ansible_check_mode`. Ansible ANDs a list `when:`, and that conjunct is true on
every real run, so adding it changes no byte. Nothing here parses a boolean expression: a
condition is compared as text against the same text respelled, and any condition that is not
matched by its counterpart keeps the file. A hunk that drops the gate keeps the file too --
that puts a task back into check mode, which is a real change.

Per hunk rather than per file, because a scalar-to-list rewrite is local. Balancing across the
whole file would read a `when: A` removed from one task against a `- A` added to another,
five hunks away, as a respelling of one condition.

The narrowing skips `manifests`, the role whose tasks render every service's bytes. Three
docstrings in `releases.py` name it as the one path that must never read clean (#947), the
argument here would hold there too, and `7fd4189cb` did not touch it -- so applying it there
buys zero unstuck services for one more thing a reviewer of #947 has to re-check.

WHY THIS RULE AND THE OTHER FOUR NARROWINGS ARE NOT REPLACED BY A DIGEST COMPARISON (#2505).
The five are #1636 and #1672 in `releases.py`, #2416 and #2436 here, and #2504 in
`releases_consumers.py`. A release record already carries `manifests_digest` for the bytes the apply
wrote, so comparing it against a fresh render would answer all five questions at once. The
repo's offline render harness cannot produce that render: it stubs SOPS values and supplies its
own placeholder `domain`, and on 2026-09-25 it reproduced 9 of 57 records. A dry run with
`-e manifests_render_record=true` can: it writes a render record whose digest comes from the
same task file as the release record's, and it reproduced all 45 services a dry run can reach
(#2574). Three gaps remain before this module can read it. A match is blind to secret
manifests, which the digest excludes (#2586). Nothing produces render records on a schedule
(#2587). 13 stamped services cannot be dry-run at all (#2588). `ansible/roles/k8s/manifests/CLAUDE.md`,
under `## Release records`, carries the measurement. Do not delete a narrowing for a digest
comparison until all three have landed.
"""

import re
import subprocess
from collections import Counter

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path -- the same bootstrap releases.py carries, for its reason.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from lib.git import git as _git

# A changed line, stripped of its diff marker and indentation, that a real run cannot see on
# its own. The `when:` forms are NOT here: they are conditions, and the comparison below is
# what decides whether the set of them moved.
_CHECK_MODE_ONLY_LINE = re.compile(r"^(?:#.*|check_mode:\s*(?:false|no))$")

# The conjunct `7fd4189cb` added, in either spelling: `when: not ansible_check_mode` on its own
# line, or `- not ansible_check_mode` as one item of a list `when:`. Both normalise to this.
_CHECK_MODE_GATE = "not ansible_check_mode"

_WHEN_KEY = re.compile(r"^when:\s*(.*)$")
_LIST_ITEM = re.compile(r"^-\s+(.+)$")

_DIFF_FILE_HEADER = re.compile(r"^diff --git a/\S+ b/(\S+)$")
_DIFF_HUNK_HEADER = re.compile(r"^@@ ")

# Appended to the diff so the loop flushes its last file on a header it already handles,
# rather than repeating the same check after the loop.
_TRAILING_HEADER = "diff --git a/. b/."


def _is_service_tasks_file(path, roles_prefix, renderer):
    """Whether `path` is a k8s service role's own `tasks/` file, renderer excluded."""
    parts = path.split("/")
    return (
        len(parts) > len(roles_prefix) + 2
        and tuple(parts[: len(roles_prefix)]) == tuple(roles_prefix)
        and parts[len(roles_prefix)] != renderer
        and parts[len(roles_prefix) + 1] == "tasks"
    )


def _hunks(body):
    """Split one file's diff body into hunks, each `[(changed, marker, text), ...]`.

    Args:
        body: the lines of a single file's diff, headers and all.

    Returns:
        One list per hunk, in file order, holding every line of that hunk with its first
        character (the `+`/`-`/space marker) stripped. `changed` is False for a context line.
        Lines before the first `@@` are dropped, which is how `index`, `old mode` and the
        `---`/`+++` pair stay out of the classifier.
    """
    hunks, current = [], None
    for line in body:
        if _DIFF_HUNK_HEADER.match(line):
            current = []
            hunks.append(current)
            continue
        if current is None or line.startswith("\\"):
            continue
        current.append((line[:1] in "+-", line[:1], line[1:]))
    return hunks


def _side(hunk, marker):
    """One side of a hunk: its context lines plus the lines carrying `marker`."""
    return [
        (changed, text) for changed, mark, text in hunk if not changed or mark == marker
    ]


def _changed_conditions(side):
    """The `when:` conditions this side of a hunk changed, and whether every other line is inert.

    Walks the side in file order, tracking whether the current line sits under a bare `when:`
    key: Ansible's list form, whose items are the conjuncts. A comment or a blank line neither
    opens nor closes that block -- authelia's added conjunct carries a comment above it.

    Returns:
        `(conditions, inert)`. `conditions` is every condition text on a CHANGED line, from a
        `when: <expr>` scalar or from a list item inside a `when:` block. `inert` is False when
        a changed line is neither a condition nor something a real run cannot see, which keeps
        the whole file counting.
    """
    conditions, inert = [], True
    when_indent = None
    for changed, raw in side:
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if when_indent is not None and not (
            indent > when_indent and text.startswith("-")
        ):
            when_indent = None
        if key := _WHEN_KEY.match(text):
            if expr := key.group(1).strip():
                if changed:
                    conditions.append(expr)
            else:
                when_indent = indent
            continue
        if when_indent is not None:
            item = _LIST_ITEM.match(text)
            if item is None:
                inert = False
            elif changed:
                conditions.append(item.group(1).strip())
            continue
        if changed and not _CHECK_MODE_ONLY_LINE.match(text):
            inert = False
    return conditions, inert


def _hunk_is_inert(hunk):
    """Whether a real deploy renders the same bytes either side of this hunk.

    True when every changed line is a comment, a blank, a `check_mode: false` or a `when:`
    condition, AND the conditions balance: the same texts on both sides, apart from
    `not ansible_check_mode`, which a real run always satisfies. A gate the hunk REMOVES
    fails the balance, because dropping it puts a task back into check mode.
    """
    removed, removed_inert = _changed_conditions(_side(hunk, "-"))
    added, added_inert = _changed_conditions(_side(hunk, "+"))
    if not (removed_inert and added_inert):
        return False
    removed, added = Counter(removed), Counter(added)
    if added[_CHECK_MODE_GATE] < removed[_CHECK_MODE_GATE]:
        return False
    del removed[_CHECK_MODE_GATE], added[_CHECK_MODE_GATE]
    return removed == added


def _diff_bodies(stdout):
    """{path: [diff lines]} for a multi-file `git diff`, one entry per file it reports."""
    bodies, path, body = {}, None, []
    for line in stdout.splitlines() + [_TRAILING_HEADER]:
        if header := _DIFF_FILE_HEADER.match(line):
            if path is not None:
                bodies[path] = body
            path, body = header.group(1), []
        elif path is not None:
            body.append(line)
    return bodies


def _check_mode_only_files(commit, paths, repo_root, ref):
    """The subset of `paths` whose whole `commit..ref` diff only changes `--check` behaviour.

    One `git diff` for every candidate at once, parsed per file: the caller already pays one
    `git log` per distinct record commit, and a call per path would multiply that by however
    many roles a sweep like `7fd4189cb` touched.

    Returns:
        The inert paths. Empty when git cannot answer, which leaves every path counting.
    """
    try:
        result = _git(
            "diff",
            # `diff.noprefix = true` in a host's git config drops the `a/`/`b/` the header
            # pattern below anchors on. Nothing would match, `inert` would stay empty and the
            # narrowing would no-op while every test here still passed -- green and checking
            # nothing. `_GIT_CLEAN_ENV` in the fixtures strips `GIT_*` but still reads
            # ~/.gitconfig, so the parse has to pin the prefixes rather than inherit them.
            "--src-prefix=a/",
            "--dst-prefix=b/",
            # `diff.context` is the same hazard for the same reason, and it is what decides
            # whether a `- <conjunct>` line has its `when:` key in view. At `diff.context = 0`
            # no added conjunct would classify, every candidate would keep counting, and the
            # narrowing would no-op silently.
            "--unified=3",
            f"{commit}..{ref}",
            "--",
            *paths,
            cwd=repo_root,
            timeout=15,
            check=True,
        )
    except OSError, subprocess.SubprocessError:
        return frozenset()
    if result.returncode != 0:
        return frozenset()
    inert = set()
    for path, body in _diff_bodies(result.stdout).items():
        hunks = _hunks(body)
        # No hunk at all means a rename or a mode change with no content line, which is not
        # evidence that nothing moved.
        if hunks and all(map(_hunk_is_inert, hunks)):
            inert.add(path)
    return frozenset(inert)


def drop_check_mode_only(paths, commit, repo_root, ref, roles_prefix, renderer):
    """`paths` minus every service `tasks/` file whose diff only changes `--check` behaviour.

    Args:
        paths: repo-relative paths, already filtered by `releases._is_real_change`.
        commit: the release record's commit -- the left end of the range.
        repo_root: the checkout whose history to read.
        ref: the right end of the range, `origin/master` in production.
        roles_prefix: `releases.K8S_ROLES_PREFIX`, the tuple naming the k8s role tree.
        renderer: `releases.MANIFEST_RENDERER`, the one role this narrowing skips.

    Returns:
        A list in the order given. Identical to `paths` when no candidate matched, which is
        also when no `git diff` runs at all.
    """
    candidates = [p for p in paths if _is_service_tasks_file(p, roles_prefix, renderer)]
    if not candidates:
        return list(paths)
    inert = _check_mode_only_files(commit, candidates, repo_root, ref)
    return [p for p in paths if p not in inert]
