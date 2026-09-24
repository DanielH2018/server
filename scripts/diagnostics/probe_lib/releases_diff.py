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
`when: not ansible_check_mode` is always true outside it, and a comment or a blank line is
never evaluated. A file whose whole diff is those is dropped. Anything else keeps the file --
a line this module does not recognise, a diff git cannot produce, a rename carrying no changed
line at all -- because a drift monitor that guesses wrong has to guess toward visible.

`check_mode: true` is deliberately not in the set. That one DOES change a real run: it puts a
task into check mode that would otherwise have made its change.

The narrowing skips `manifests`, the role whose tasks render every service's bytes. Three
docstrings in `releases.py` name it as the one path that must never read clean (#947), the
argument here would hold there too, and `7fd4189cb` did not touch it -- so applying it there
buys zero unstuck services for one more thing a reviewer of #947 has to re-check.
"""

import re
import subprocess

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path -- the same bootstrap releases.py carries, for its reason.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from lib.git import git as _git

# A changed line, stripped of its diff marker and indentation, that a real run cannot see.
_CHECK_MODE_ONLY_LINE = re.compile(
    r"^(?:#.*|check_mode:\s*(?:false|no)|when:\s*not\s+ansible_check_mode)$"
)

_DIFF_FILE_HEADER = re.compile(r"^diff --git a/\S+ b/(\S+)$")

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
    path, changed = None, []
    for line in result.stdout.splitlines() + [_TRAILING_HEADER]:
        header = _DIFF_FILE_HEADER.match(line)
        if header:
            # `changed` empty means a rename or a mode change with no content line at all,
            # which is not evidence that nothing moved.
            if path is not None and changed and all(map(_is_inert, changed)):
                inert.add(path)
            path, changed = header.group(1), []
        elif (
            path is not None
            and line.startswith(("+", "-"))
            and not line.startswith(("+++", "---"))
        ):
            changed.append(line[1:].strip())
    return frozenset(inert)


def _is_inert(text):
    return not text or bool(_CHECK_MODE_ONLY_LINE.match(text))


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
