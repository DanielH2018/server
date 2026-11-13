"""The paths a Renovate PR may change only with the operator's approval, and its exceptions.

dotfiles#967 slice 1. Renovate's content is app-authored version bumps, so approving every PR
would block routine updates. A PR needs the operator's approval of its head only when it
changes a path on the T2 approval floor other than by a digest-only image bump, or when anyone but
`renovate[bot]` pushed to its branch. The lander also refuses a soak status that
`renovate[bot]` did not post.

Stdlib only, like the rest of the agent. Every input is a GitHub API answer the lander read
with the operator's token, at the head SHA it pins land.sh to.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

# The T2 approval floor this component ships, which every T2 lander reads (A5a).
FLOOR_FILE = "/usr/lib/homelab/t2-approval-floor"
RENOVATE_BOT = "renovate[bot]"
SOAK_CONTEXT = "renovate/stability-days"
# The only shape the digest-only exception admits: a host component's defaults.
_DIGEST_FILE = "hosts/components/*/defaults/main.yml"
_IMAGE_LINE = re.compile(r"^\s*[A-Za-z0-9_]+_image:\s")
_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}")


def read_floor(path: str = FLOOR_FILE) -> tuple[str, ...]:
    """The entries in the floor file at `path`, without blank lines and `#` comments.

    Raises:
        OSError: the file cannot be read.
        ValueError: the file names no entry. An empty floor would approve every path, so it
            is refused rather than read as "nothing needs approval".
    """
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    entries = tuple(
        s for s in (line.strip() for line in lines) if s and not s.startswith("#")
    )
    if not entries:
        raise ValueError(f"{path} names no path")
    return entries


def on_floor(path: str, floor: tuple[str, ...]) -> bool:
    """Whether `path` is under an entry of `floor`.

    A plain entry is a path prefix. An entry holding `*` is a glob over the whole path, where
    `**` spans directories. `land_lib/policy.py:approval_hits` matches the same way.
    """
    for entry in floor:
        if "*" in entry:
            if PurePosixPath(path).full_match(entry):
                return True
        elif path.startswith(entry):
            return True
    return False


def _changed_lines(patch: str) -> tuple[list[str], list[str]]:
    removed = [line[1:] for line in patch.splitlines() if line.startswith("-")]
    added = [line[1:] for line in patch.splitlines() if line.startswith("+")]
    return removed, added


def digest_only(entry: dict) -> bool:
    """Whether a REST pulls-files `entry` changes only `@sha256:` digests of `*_image:` lines.

    Every removed line pairs, in order, with an added line that differs from it only in its
    digest. A file GitHub sends no patch for is not digest-only, because nothing shows what
    it changes.
    """
    path = entry.get("filename") or ""
    patch = entry.get("patch")
    if entry.get("status") != "modified" or not isinstance(patch, str):
        return False
    if not PurePosixPath(path).full_match(_DIGEST_FILE):
        return False
    removed, added = _changed_lines(patch)
    if not removed or len(removed) != len(added):
        return False
    for old, new in zip(removed, added, strict=True):
        if not (_IMAGE_LINE.match(old) and _IMAGE_LINE.match(new)):
            return False
        if old == new or _DIGEST.sub("", old) != _DIGEST.sub("", new):
            return False
        if len(_DIGEST.findall(old)) != 1 or len(_DIGEST.findall(new)) != 1:
            return False
    return True


def floor_hits(files: list[dict], floor: tuple[str, ...]) -> list[str]:
    """Each changed path on the floor that is not a digest-only bump, in order.

    A rename is checked under its old path too, so a rename out of a floor path counts.
    """
    hits: list[str] = []
    for entry in files:
        for name in (entry.get("filename"), entry.get("previous_filename")):
            if (
                name
                and name not in hits
                and on_floor(name, floor)
                and not digest_only(entry)
            ):
                hits.append(name)
    return hits


def pusher_problem(events: list[dict], head: str) -> str:
    """Why the branch's pushes are not all `renovate[bot]`'s up to `head`; empty when they are.

    `events` is the repository activity API's answer for the branch's ref, newest first.
    Renovate reuses branch names, so only the events since the newest `branch_creation` count.
    It fails closed when no creation is in the API's window.
    """
    since: list[dict] = []
    for event in events:
        since.append(event)
        if event.get("activity_type") == "branch_creation":
            break
    else:
        return "no branch creation is in the activity the API returned"
    others = sorted(
        {
            (e.get("actor") or {}).get("login") or "an unknown account"
            for e in since
            if (e.get("actor") or {}).get("login") != RENOVATE_BOT
        }
    )
    if others:
        return f"{', '.join(others)} pushed to the branch"
    if since[0].get("after") != head:
        return f"the newest push is not the head {head[:8]}"
    return ""


def soak_problem(statuses: list[dict]) -> str:
    """Why the soak status does not pass; empty when it is absent or `renovate[bot]`'s success.

    `statuses` is the commit statuses API's answer for the head, newest first, so the first
    one with the soak context is its current state. A status any other account posted counts
    as a failure, because the soak is Renovate's own.
    """
    for status in statuses:
        if status.get("context") != SOAK_CONTEXT:
            continue
        creator = (status.get("creator") or {}).get("login")
        if creator != RENOVATE_BOT:
            return (
                f"its {SOAK_CONTEXT} status was posted by {creator}, not {RENOVATE_BOT}"
            )
        if status.get("state") != "success":
            return f"its {SOAK_CONTEXT} status has not passed"
        return ""
    return ""
