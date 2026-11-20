"""The census row holding every host component that ships a timer to an autonomous-role contract."""

import re
from pathlib import PurePosixPath

from _component_contract import HEADING, sections
from _helpers import REPO
from _row_table import Census, Subject, tracked
from lib import yaml_fast

_UNIT_DIR = PurePosixPath("/usr/lib/systemd/system")
_FIELDS = {
    "Scope": r"scope\b",
    "Mode": r"mode\b",
    "Abort valves": r"abort valves?\b",
    "Evidence": r"(required )?evidence\b",
}
# A contract heading written as a manifest comment, which a pointer sentence never opens with.
_COMMENT_CONTRACT = re.compile(rf"^#\s*{HEADING}", re.MULTILINE)
# A bullet's label, in a `- **Scope:**` or a plain `- Scope:` doc line.
_FIELD_LABEL = re.compile(r"^\s*- \*{0,2}([^:*,(\n]+)[:*,(]", re.MULTILINE)


def _timers(subject: Subject) -> list[str]:
    files = (yaml_fast.safe_load(subject.text) or {}).get("files") or []
    destinations = [PurePosixPath(str(entry.get("dst", ""))) for entry in files]
    return [
        d.name for d in destinations if d.parent == _UNIT_DIR and d.suffix == ".timer"
    ]


def _missing_fields(contract: str) -> list[str]:
    """The fields a contract's text states no `- <field>` bullet for."""
    labels = _FIELD_LABEL.findall(contract)
    return [
        field
        for field, pattern in _FIELDS.items()
        if not any(re.match(pattern, label, re.I) for label in labels)
    ]


def doc_gaps(doc: str) -> list[str] | None:
    """What a `CLAUDE.md`'s contract sections lack, None when it has no such section.

    Each section answers for itself, because a doc can hold one contract per timer.
    """
    found = sections(doc)
    if not found:
        return None
    return sorted({field for s in found for field in _missing_fields(s)})


def _uncontracted_timers(subject: Subject) -> list[str]:
    timers = _timers(subject)
    if not timers:
        return []
    if _COMMENT_CONTRACT.search(subject.text):
        return [
            f"states its {HEADING} in a component.yml comment, not in its CLAUDE.md"
        ]
    path = (REPO / subject.rel).parent / "CLAUDE.md"
    gaps = doc_gaps(path.read_text()) if path.is_file() else None
    if gaps is None:
        return [f"ships {', '.join(timers)} and states no {HEADING}"]
    if gaps:
        return [f"ships {', '.join(timers)} and its contract lacks {', '.join(gaps)}"]
    return []


_TIMER_FILE = "files:\n  - {src: units/x.timer, dst: /usr/lib/systemd/system/x.timer}\n"

COMPONENT_TIMERS_CARRY_A_CONTRACT = Census(
    name="component-timers-carry-a-contract",
    reason=(
        "A host component's timer acts with no human in the loop, so every "
        "`## Autonomous-role contract` section of the component's `CLAUDE.md` states a "
        "`- <field>` bullet each for the scope, mode, abort valves and evidence. Naming the "
        "phrase alone does not count. A `# Autonomous-role contract` comment in "
        "`component.yml` is flagged even beside a `CLAUDE.md` contract, because the two copies "
        "drift. A timer that changes no state goes in `allow` with that reason."
    ),
    files=lambda: tracked("hosts/components/*/component.yml"),
    offence=_uncontracted_timers,
    count=lambda subject: 1 if _timers(subject) else 0,
    red=(
        Subject("hosts/components/fixture/component.yml", _TIMER_FILE),
        Subject(
            "hosts/components/board-sync/component.yml",
            f"# {HEADING} (a).\n# - Scope: one file.\n{_TIMER_FILE}",
        ),
    ),
    green=(
        Subject("hosts/components/board-sync/component.yml", _TIMER_FILE),
        Subject(
            "hosts/components/fixture/component.yml",
            "files:\n  - {src: a.sh, dst: /usr/lib/homelab/a}\n",
        ),
    ),
    min_matches=15,
    must_find=frozenset(
        {
            "hosts/components/claude-code/component.yml",
            "hosts/components/docs-refresh/component.yml",
            "hosts/components/secret-rotation/component.yml",
        }
    ),
    allow={
        "hosts/components/nut-secondary/component.yml": (
            "homelab-ups-watchdog.timer only reads and reports; upsmon is the actor that "
            "changes state, and k8s/roles/nut/CLAUDE.md documents it"
        ),
    },
)
