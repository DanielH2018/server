"""The file-level collision check `fanout_place.py launch` runs before it touches a host.

The triage step groups issues so no two batches share an Ansible role. That rule never covered
the shared code under `scripts/`: on the 2026-09-11 wave two batches with different role
groupings both cited `scripts/diagnostics/probe_lib/alerts.py`, two agents fixed the same
defect with a character-identical regex, and the second PR had to be superseded by hand
(#1798). The check reads the same body citations the orchestrator groups by, so a grouping
that missed one is refused here rather than discovered at merge.
"""

import sys
from collections.abc import Mapping, Sequence

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from fanout_lib.brief import Issue
from findings_lib.issue_model import cited_paths


def shared_files(
    batches: Mapping[str, Sequence[int]], issues: Mapping[int, Issue]
) -> list[tuple[str, list[str]]]:
    """Every file cited by issues in two or more batches, with the batches that cite it.

    Args:
        batches: batch id → the issue numbers in it, as `_parse_batches` returns them.
        issues: every fetched issue by number; a number a batch names but this lacks is
            skipped, since `_fetch_issues` has already refused that launch.

    Returns:
        `[(path, [batch, ...]), ...]` sorted by path, each batch list sorted. Empty when the
        grouping is clean.
    """
    cited: dict[str, set[str]] = {}
    for batch, numbers in batches.items():
        for number in numbers:
            issue = issues.get(number)
            if issue is None:
                continue
            for path in cited_paths(issue.body):
                cited.setdefault(path, set()).add(batch)
    return [
        (path, sorted(names)) for path, names in sorted(cited.items()) if len(names) > 1
    ]


def refuse_shared_files(
    batches: Mapping[str, Sequence[int]], issues: Mapping[int, Issue], allowed: bool
) -> bool:
    """Whether `launch` must refuse this grouping, having printed every collision to stderr.

    Runs before the first ssh, like the label gate: a refusal here costs nothing. `allowed`
    is the `--allow-shared-files` override for a citation that is context rather than an
    edit target; the collisions still print, so the override is a decision on the record.
    """
    collisions = shared_files(batches, issues)
    for path, names in collisions:
        print(
            f"launch: batches {', '.join(names)} all cite {path} — two agents editing one "
            "file is the #1798 conflict; regroup them into one batch",
            file=sys.stderr,
        )
    if collisions and allowed:
        print("launch: --allow-shared-files set, launching anyway", file=sys.stderr)
    return bool(collisions) and not allowed
