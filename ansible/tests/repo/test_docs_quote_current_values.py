"""A value a hand-written doc quotes mid-sentence must still equal the tree.

The docs site has two kinds of page. The reference pages under `docs/reference/` are
generated, so a tunable they show is read from the tree on every cron run. Every other page
is prose, and prose quotes tunables too: "retain 14", "every 10 minutes", "a 180-second
restart window". Nothing re-derives those. The tunable moves in a role default or a Python
constant, the sentence that quoted it is not in the diff, and the page reads confidently
wrong until someone notices. Measured 2026-09-02: five quoted values were stale, among them
a weekly Longhorn retain of 4 (lowered to 2 on 2026-08-17) and a GitOps tick of 30 minutes
(10 since 2026-09-01), both on pages an operator reads during a recovery.

`test_documented_paths_exist.py` guards the NAMES a doc cites. This guards the VALUES. The
two are deliberately separate: a name is extracted by one regex over every doc, while a
value has no shape of its own -- "14" is a retain count on one line and a port on the next
-- so each guarded value is a row here: the page, a regex whose single capture group is the
quoted value, and a reader that returns what the tree says now.

The table is hand-maintained, and that is the trade. A row is added when a value has rotted
once or sits next to one that did; a table that tried to cover every number would be a
second copy of the docs. Prefer the generated fragments under `docs/assets/generated/` for a
whole table of tunables, and a row here for a single figure inside a sentence.

Two failure shapes, and both must fail. A regex that finds nothing means the sentence was
reworded and the guard is no longer reading anything -- that is flagged, not skipped,
because a guard that silently stops matching is the shape this repo has paid for before
(`volume-claim`'s short-circuit, `image-smoke`'s bare boot). `mismatches()` is the pure
core, and the paired `_is_clean` / `_is_flagged` tests below prove it can go red.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from _helpers import REPO, load_yaml

K3S_DEFAULTS = REPO / "ansible/roles/setup/k3s/defaults/main.yml"
GITOPS_DEFAULTS = REPO / "ansible/roles/setup/gitops_deploy/defaults/main.yml"
PROBE_HEALTH = REPO / "scripts/diagnostics/probe_health.py"
SECRET_ROTATION = REPO / "scripts/secrets_mgmt/secret_rotation.py"


def _k3s_default(name: str) -> Callable[[], str]:
    return lambda: str(load_yaml(K3S_DEFAULTS)[name])


def _gitops_tick_minutes() -> str:
    # systemd span, `10min`. The doc quotes the number of minutes.
    value = str(load_yaml(GITOPS_DEFAULTS)["gitops_deploy_tick_interval"])
    match = re.fullmatch(r"(\d+)min", value)
    assert match, (
        f"gitops_deploy_tick_interval is {value!r}, not a whole number of minutes"
    )
    return match.group(1)


def _python_constant(path: Path, name: str) -> Callable[[], str]:
    # Read by regex rather than import: probe_health.py and secret_rotation.py bootstrap
    # sys.path and read the environment on import, and a value guard has no business
    # running either.
    def read() -> str:
        match = re.search(rf"^{name} = (\d+)$", path.read_text(), re.MULTILINE)
        assert match, f"{path.relative_to(REPO)}: no `{name} = <int>` line"
        return match.group(1)

    return read


def _tier_days(tier: str) -> Callable[[], str]:
    def read() -> str:
        match = re.search(
            rf'^\s+"{tier}": (\d+),$', SECRET_ROTATION.read_text(), re.MULTILINE
        )
        assert match, f"secret_rotation.py: no TIER_DAYS entry for {tier!r}"
        return match.group(1)

    return read


@dataclass(frozen=True)
class Row:
    doc: str
    pattern: str  # exactly one capture group: the quoted value
    source: str  # named in the failure, so the fix is one hop away
    reader: Callable[[], str]

    def __str__(self) -> str:
        return f"{self.doc} ~ /{self.pattern}/"


ROWS: list[Row] = [
    Row(
        "docs/longhorn-disaster-recovery.md",
        r"\*\*Daily tier\*\* \(the four R2 volumes, retain (\d+)\)",
        "k3s_longhorn_backup_retain",
        _k3s_default("k3s_longhorn_backup_retain"),
    ),
    Row(
        "docs/longhorn-disaster-recovery.md",
        r"\*\*Weekly tier\*\* \(all \d+ B2 volumes[^)]*, retain (\d+)\)",
        "k3s_longhorn_weekly_backup_retain",
        _k3s_default("k3s_longhorn_weekly_backup_retain"),
    ),
    Row(
        "docs/gitops-pipeline.md",
        r"every (\d+) minutes \(`gitops_deploy_tick_interval`",
        "gitops_deploy_tick_interval",
        _gitops_tick_minutes,
    ),
    Row(
        "docs/deploying.md",
        r"a (\d+)-second restart window",
        "probe_health.RECENT_RESTART_SECONDS",
        _python_constant(PROBE_HEALTH, "RECENT_RESTART_SECONDS"),
    ),
    Row(
        "docs/claude-tooling.md",
        r"restarted in the last (\d+)s\.",
        "probe_health.RECENT_RESTART_SECONDS",
        _python_constant(PROBE_HEALTH, "RECENT_RESTART_SECONDS"),
    ),
    Row(
        "docs/secret-rotation.md",
        r"`ROTATE_LEAD_DAYS` = (\d+)",
        "secret_rotation.ROTATE_LEAD_DAYS",
        _python_constant(SECRET_ROTATION, "ROTATE_LEAD_DAYS"),
    ),
    *(
        Row(
            "docs/secret-rotation.md",
            rf"^\| `{tier}` \| (\d+) d \|",
            f"secret_rotation.TIER_DAYS[{tier!r}]",
            _tier_days(tier),
        )
        for tier in ("auto", "assisted", "external", "pinned")
    ),
]


def mismatches(text: str, pattern: str, expected: str) -> list[str]:
    """Every way `text` fails to quote `expected` at `pattern`. Empty means clean.

    A pattern that matches nowhere is a failure in its own right: the sentence moved and
    the guard would otherwise pass on nothing.
    """
    regex = re.compile(pattern, re.MULTILINE)
    assert regex.groups == 1, f"pattern must have exactly one capture group: {pattern}"
    found = regex.findall(text)
    if not found:
        return [f"nothing matches /{pattern}/ -- reworded? update the row or the doc"]
    return [
        f"quotes {value}, tree says {expected}" for value in found if value != expected
    ]


@pytest.mark.parametrize("row", ROWS, ids=str)
def test_the_doc_quotes_what_the_tree_says(row: Row):
    text = (REPO / row.doc).read_text()
    problems = mismatches(text, row.pattern, row.reader())
    assert not problems, f"{row.doc} vs {row.source}: " + "; ".join(problems)


def test_every_row_names_a_file_that_exists():
    missing = [row.doc for row in ROWS if not (REPO / row.doc).is_file()]
    assert not missing, missing


# --- red-proof pairs for the pure core ------------------------------------------------


def test_a_current_value_is_clean():
    assert mismatches("retain 14 today", r"retain (\d+)", "14") == []


def test_a_stale_value_is_flagged():
    problems = mismatches("retain 4 today", r"retain (\d+)", "2")
    assert problems == ["quotes 4, tree says 2"]


def test_a_reworded_sentence_is_flagged_not_skipped():
    problems = mismatches("keeps four backups", r"retain (\d+)", "2")
    assert len(problems) == 1
    assert "nothing matches" in problems[0]


def test_every_occurrence_is_checked_not_only_the_first():
    problems = mismatches("retain 2, later retain 4", r"retain (\d+)", "2")
    assert problems == ["quotes 4, tree says 2"]


def test_a_pattern_without_a_capture_group_is_rejected():
    with pytest.raises(AssertionError, match="exactly one capture group"):
        mismatches("retain 2", r"retain \d+", "2")
