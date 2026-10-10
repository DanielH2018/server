"""The file-level collision check `fanout_place.py launch` runs before it touches a host.

The triage step groups issues so no two batches share an Ansible role. That rule never covers
the shared code under `scripts/`: two batches with different role groupings can both cite
`scripts/diagnostics/probe_lib/alerts.py`, and two agents then fix the same defect. The check
reads the same body citations the orchestrator groups by, so a grouping that missed one is
refused here rather than discovered at merge.
"""

import sys
from collections.abc import Collection, Mapping, Sequence

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from fanout_lib.brief import Issue
from findings_lib.issue_model import cited_paths
from findings_lib.solo_only import fanout_tooling_paths
from findings_lib.tracked_paths import resolve_fragments


def shared_files(
    batches: Mapping[str, Sequence[int]],
    issues: Mapping[int, Issue],
    tracked: Collection[str] = (),
) -> list[tuple[str, list[str]]]:
    """Every file cited by issues in two or more batches, with the batches that cite it.

    Args:
        batches: batch id → the issue numbers in it, as `_parse_batches` returns them.
        issues: every fetched issue by number; a number a batch names but this lacks is
            skipped, since `_fetch_issues` has already refused that launch.
        tracked: the target repo's tracked files, which a path cited from a subdirectory
            resolves against (#4232). Empty leaves every path as the body spells it.

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
            for path in resolve_fragments(cited_paths(issue.body), tracked):
                cited.setdefault(path, set()).add(batch)
    return [
        (path, sorted(names)) for path, names in sorted(cited.items()) if len(names) > 1
    ]


def refuse_shared_files(
    batches: Mapping[str, Sequence[int]],
    issues: Mapping[int, Issue],
    allowed: Sequence[str] = (),
    tracked: Collection[str] = (),
) -> bool:
    """Whether `launch` must refuse this grouping, having printed every collision to stderr.

    Runs before the first ssh, like the label gate: a refusal here costs nothing. `allowed`
    is the `--allow-shared-file PATH` override, one path per use, for a citation that is
    context rather than an edit target — `docs/claude-tooling.md` cited by three findings
    that each edit a different script. It names the file it excuses, so a wave that trips on
    a doc path cannot switch the check off for the script collision beside it; that collision
    still refuses. An excused collision still prints, so the override is on the record.
    """
    collisions = shared_files(batches, issues, tracked)
    refused = False
    for path, names in collisions:
        if path in allowed:
            print(
                f"launch: batches {', '.join(names)} all cite {path} — allowed by "
                "--allow-shared-file",
                file=sys.stderr,
            )
            continue
        refused = True
        print(
            f"launch: batches {', '.join(names)} all cite {path} — two agents editing one "
            "file is the #1798 conflict; regroup them into one batch, or pass "
            f"--allow-shared-file {path} if the citation is context rather than an edit",
            file=sys.stderr,
        )
    return refused


def refuse_solo_only(
    batches: Mapping[str, Sequence[int]], issues: Mapping[int, Issue]
) -> bool:
    """Whether `launch` must refuse because a batch holds an issue citing the fan-out tooling.

    `next` marks such an issue `[solo-only]` (#3959). A batch that edits the pipeline runs
    under its own edited copy of the reviewer, the red gate and the Stop hook, which is why
    the held-settings machinery exists. Refused before the first ssh, like the shared-file
    check, and a solo session claims and works the issue instead.
    """
    refused = False
    for batch, numbers in batches.items():
        for number in numbers:
            issue = issues.get(number)
            cited = fanout_tooling_paths(issue.body) if issue else []
            if not cited:
                continue
            refused = True
            print(
                f"launch: batch {batch}: #{number} cites the fan-out tooling "
                f"({', '.join(cited)}) — it is solo-only (#3959); drop it from the batch "
                "and work it in a session of its own",
                file=sys.stderr,
            )
    return refused
