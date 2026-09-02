"""Tree-wide guard: only a Backup CR query may read `.status.volumeName`.

WHY THIS EXISTS. Longhorn puts `.status.volumeName` on **Backup** CRs and not on **Volume** CRs.
A jq or jsonpath expression that reads it off a Volume gets `null` — and `null` compares equal
to nothing, so the filter silently matches an empty set. There is no error and no empty-result
warning; the caller just proceeds with nothing.

It has already cost a night's work. `longhorn-restore-drill.sh.j2` compared a Volume's
`.status.volumeName` against the backed-up set and left the drill with an empty candidate set
every night. The fix is recorded in a comment at that line, which is where this rule comes from:

    # .metadata.name, NOT .status.volumeName: a Volume CR has no status.volumeName — that
    # field belongs to Backup CRs, and comparing the null it yields here matched nothing at
    # all, leaving the drill with an empty candidate set every night.

WHY THE NEAREST PRECEDING `get` DECIDES. These are shell pipelines: the resource is named by the
`kubectl get` and the expression that reads it is piped from that same command. So the resource
governing any `.status.volumeName` is the one in the nearest `get <resource>.longhorn.io` above
it. That is the actual semantics, not an approximation of it.

Comment lines are skipped deliberately. Three of the live mentions are prose explaining this very
trap, and a guard that flagged its own documentation would be untenable — the first person to hit
it would delete the explanation to make the check pass.

WHAT THE REAL-TREE ASSERTION IS WORTH. Zero violations today, so it passing is not evidence the
rule works. The synthetic pairs are that evidence, and the guard was additionally observed
failing against a violation injected into a real template.
"""

from __future__ import annotations

import re
from pathlib import Path

from _helpers import REPO as _REPO_ROOT
from _helpers import ROLES as _ROLES

_ANSIBLE = _ROLES.parent

# `kubectl get <plural>.longhorn.io`, capturing which resource the pipeline is reading.
_GET = re.compile(r"\bget\s+([a-z]+)\.longhorn\.io\b")
_READS_VOLUMENAME = re.compile(r"\.status\.volumeName\b")
_COMMENT = re.compile(r"^\s*#")

# The one resource that carries the field. Anything else reading it gets null.
_CARRIES_VOLUMENAME = "backups"


def volume_cr_volumename_reads(text: str) -> list[int]:
    """Line numbers reading `.status.volumeName` under a non-Backup Longhorn `get`.

    A read with NO preceding `get ...longhorn.io` at all is not flagged: it is reading something
    this guard cannot attribute, and guessing would make the rule fire on unrelated code.
    """
    hits = []
    resource = None
    for i, line in enumerate(text.splitlines(), start=1):
        if _COMMENT.match(line):
            continue
        found = _GET.search(line)
        if found:
            resource = found.group(1)
        if _READS_VOLUMENAME.search(line) and resource not in (
            None,
            _CARRIES_VOLUMENAME,
        ):
            hits.append(i)
    return hits


def _candidate_files() -> list[Path]:
    roots = (_ROLES, _ANSIBLE)
    seen: dict[Path, None] = {}
    for root in roots:
        for pattern in ("**/*.j2", "**/*.yml"):
            for p in root.glob(pattern):
                if "archive" not in p.parts and p.is_file():
                    seen.setdefault(p, None)
    return sorted(seen)


def test_no_volume_cr_query_reads_volumename() -> None:
    """The real tree. See the module docstring on what this passing does and does not prove."""
    offenders = []
    for path in _candidate_files():
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        for line_no in volume_cr_volumename_reads(text):
            offenders.append(f"{path.relative_to(_REPO_ROOT)}:{line_no}")

    assert not offenders, (
        "these read `.status.volumeName` from a Longhorn resource that does not carry it — "
        "the field belongs to Backup CRs, and off a Volume it yields null, which compares "
        "equal to nothing and silently matches an empty set. Use `.metadata.name` for a "
        "Volume:\n  " + "\n  ".join(offenders)
    )


def test_a_volume_query_reading_volumename_is_flagged() -> None:
    """The 2026 restore-drill shape: the field read off the wrong resource."""
    text = (
        "VOLS=$($KUBECTL get volumes.longhorn.io -o json \\\n"
        "  | jq '[.items[] | select(.status.volumeName == $v)]')\n"
    )
    assert volume_cr_volumename_reads(text) == [2]


def test_a_backup_query_reading_volumename_is_clean() -> None:
    """The correct use. Backup CRs are the ones that carry the field."""
    text = (
        "BACKED=$($KUBECTL get backups.longhorn.io -o json \\\n"
        "  | jq '[.items[] | select(.status.state == \"Completed\") | .status.volumeName]')\n"
    )
    assert volume_cr_volumename_reads(text) == []


def test_the_nearest_preceding_get_decides() -> None:
    """A later Volume query must not be excused by an earlier Backup one."""
    text = (
        "A=$($KUBECTL get backups.longhorn.io -o json | jq '.items[].status.volumeName')\n"
        "B=$($KUBECTL get volumes.longhorn.io -o json | jq '.items[].status.volumeName')\n"
    )
    assert volume_cr_volumename_reads(text) == [2]


def test_a_comment_explaining_the_trap_is_not_flagged() -> None:
    """Three live mentions are prose about this exact trap; flagging them would delete them."""
    text = (
        "VOLS=$($KUBECTL get volumes.longhorn.io -o json \\\n"
        "  # .metadata.name, NOT .status.volumeName: a Volume CR has no status.volumeName\n"
        "  | jq '[.items[] | .metadata.name]')\n"
    )
    assert volume_cr_volumename_reads(text) == []


def test_a_read_with_no_attributable_get_is_not_flagged() -> None:
    """Unattributable is not the same as wrong; guessing would fire on unrelated code."""
    text = 'echo "{{ item.status.volumeName }}"\n'
    assert volume_cr_volumename_reads(text) == []


def test_a_volume_query_reading_metadata_name_is_clean() -> None:
    """The prescribed fix must not itself trip the rule."""
    text = (
        "$KUBECTL get volumes.longhorn.io -o json | jq '[.items[] | .metadata.name]'\n"
    )
    assert volume_cr_volumename_reads(text) == []
