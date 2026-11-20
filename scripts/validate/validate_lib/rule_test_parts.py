"""Split a promtool rule-test file into parts that `promtool test rules` can run at once.

promtool runs a file's test groups one after another, and each group evaluates every rule at
every `evaluation_interval` step up to its last `eval_time`. host-checks.test.yaml's 57 groups
took 121s in one call on 2026-11-08, one 175h group 56s of it. Each group gets its own
storage and shares nothing with the others, so the same groups spread over several files and
run in parallel check exactly what the one file does.

A part keeps each group's text byte for byte, so promtool parses what it would have parsed in
the source. Groups that name the same YAML anchor, by `&name` or `*name`, stay in one part in
their source order, because an alias resolves only against an anchor earlier in its own file.
A name that only looks like an anchor, inside a string, merges groups that need not be merged,
which costs time and nothing else. The part's header is the source's other top-level keys,
with `rule_files` made absolute, because promtool resolves them against the test file's own
directory and a part is written somewhere else.
"""

import re
from pathlib import Path

import yaml

from lib import yaml_fast

TESTS_KEY = "tests:\n"
GROUP_START = "  - "
ANCHOR_NAME = re.compile(r"[&*]([A-Za-z0-9_-]+)")
DURATION_PART = re.compile(r"(\d+)(ms|s|m|h|d|w|y)")
SECONDS = {
    "ms": 0.001,
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
    "y": 31536000,
}


def _group_starts(lines: list[str], count: int) -> list[int] | None:
    """The line index of each test group, or None when the text does not show `count` of them.

    Each group must open with a `  - ` line under a top-level `tests:` that no later top-level
    key follows.
    """
    if TESTS_KEY not in lines:
        return None
    after = lines.index(TESTS_KEY) + 1
    if any(line[:1] not in ("", " ", "#", "\n") for line in lines[after:]):
        return None
    starts = [i for i in range(after, len(lines)) if lines[i].startswith(GROUP_START)]
    return starts if len(starts) == count else None


def group_cost(group: object) -> float:
    """A group's latest `eval_time` in seconds, which sets how many steps promtool evaluates."""
    if not isinstance(group, dict):
        return 0.0
    checks = [
        *(group.get("alert_rule_test") or []),
        *(group.get("promql_expr_test") or []),
    ]
    return max(
        (
            sum(
                int(n) * SECONDS[unit]
                for n, unit in DURATION_PART.findall(str(c.get("eval_time", "")))
            )
            for c in checks
            if isinstance(c, dict)
        ),
        default=0.0,
    )


def anchor_clusters(blocks: list[str]) -> list[list[int]]:
    """The indices of `blocks`, grouped so that blocks naming a common anchor share a group.

    Returns:
        Each cluster's indices in ascending order, the clusters ordered by their first index.
    """
    owner = list(range(len(blocks)))

    def root(i: int) -> int:
        while owner[i] != i:
            owner[i] = owner[owner[i]]
            i = owner[i]
        return i

    first_named = {}
    for i, block in enumerate(blocks):
        for name in ANCHOR_NAME.findall(block):
            j = first_named.setdefault(name, i)
            owner[root(i)] = root(j)
    clusters = {}
    for i in range(len(blocks)):
        clusters.setdefault(root(i), []).append(i)
    return sorted(clusters.values())


def split_rule_test(
    source: Path, parts: int, out_dir: Path
) -> list[tuple[Path, list[int]]]:
    """Write `source`'s test groups into at most `parts` files under `out_dir`.

    The anchor clusters go whole, costliest first, each to the part with the least cost so far
    (`group_cost`), so the longest groups do not share a part.

    Args:
        source: A promtool rule-test file.
        parts: The most files to write, usually the number of parallel runs.
        out_dir: An existing directory the parts are written to.

    Returns:
        Each file to run and the 0-based indices of the `tests:` groups it holds. `source`
        comes back alone, holding every group, when there is nothing to split or its groups
        cannot be matched one for one against its parsed `tests:` list.
    """
    text = source.read_text()
    doc = yaml_fast.safe_load(text)
    groups = doc.get("tests") if isinstance(doc, dict) else None
    if not isinstance(groups, list):
        return [(source, [])]
    lines = text.splitlines(keepends=True)
    starts = _group_starts(lines, len(groups))
    if starts is None or parts < 2 or len(groups) < 2:
        return [(source, list(range(len(groups))))]
    blocks = [
        "".join(lines[start:end]).rstrip("\n") + "\n"
        for start, end in zip(starts, [*starts[1:], len(lines)], strict=True)
    ]
    costs = [group_cost(group) for group in groups]
    assigned: list[list[int]] = [[] for _ in range(parts)]
    load = [0.0] * parts
    clusters = sorted(anchor_clusters(blocks), key=lambda c: -sum(costs[i] for i in c))
    for cluster in clusters:
        part = load.index(min(load))
        assigned[part].extend(cluster)
        load[part] += sum(costs[i] for i in cluster)
    header = {key: value for key, value in doc.items() if key != "tests"}
    header["rule_files"] = [
        str((source.parent / rule_file).resolve())
        for rule_file in doc.get("rule_files") or []
    ]
    stem = source.name.removesuffix(".test.yaml")
    written = []
    for part, indices in enumerate(i for i in assigned if i):
        indices.sort()
        path = out_dir / f"{stem}.part{part}.test.yaml"
        path.write_text(
            yaml.safe_dump(header, sort_keys=False)
            + TESTS_KEY
            + "".join(blocks[i] for i in indices)
        )
        written.append((path, indices))
    if len(written) == 1:
        return [(source, written[0][1])]
    return written
