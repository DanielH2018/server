"""The census row holding every host component that ships a timer to an autonomous-role contract."""

from pathlib import PurePosixPath

from _helpers import REPO
from _row_table import Census, Subject, tracked
from lib import yaml_fast

_UNIT_DIR = PurePosixPath("/usr/lib/systemd/system")
_CONTRACT = "Autonomous-role contract"


def _timers(subject: Subject) -> list[str]:
    files = (yaml_fast.safe_load(subject.text) or {}).get("files") or []
    destinations = [PurePosixPath(str(entry.get("dst", ""))) for entry in files]
    return [
        d.name for d in destinations if d.parent == _UNIT_DIR and d.suffix == ".timer"
    ]


def _has_contract(subject: Subject) -> bool:
    if any(
        line.startswith("#") and _CONTRACT in line for line in subject.text.splitlines()
    ):
        return True
    doc = (REPO / subject.rel).parent / "CLAUDE.md"
    return doc.is_file() and f"## {_CONTRACT}" in doc.read_text()


def _uncontracted_timers(subject: Subject) -> list[str]:
    if not _timers(subject) or _has_contract(subject):
        return []
    return [f"ships {', '.join(_timers(subject))} and states no {_CONTRACT}"]


_TIMER_FILE = "files:\n  - {src: units/x.timer, dst: /usr/lib/systemd/system/x.timer}\n"
_FIXTURE = "hosts/components/fixture/component.yml"

COMPONENT_TIMERS_CARRY_A_CONTRACT = Census(
    name="component-timers-carry-a-contract",
    reason=(
        "A host component's timer acts with no human in the loop, so its component states the "
        "scope, mode, abort valves and evidence: a `# Autonomous-role contract` comment in "
        "`component.yml`, or a `## Autonomous-role contract` heading in its `CLAUDE.md`. A "
        "timer that changes no state goes in `allow` with that reason."
    ),
    files=lambda: tracked("hosts/components/*/component.yml"),
    offence=_uncontracted_timers,
    count=lambda subject: 1 if _timers(subject) else 0,
    red=(Subject(_FIXTURE, _TIMER_FILE),),
    green=(
        Subject(_FIXTURE, f"# {_CONTRACT} (what it may do).\n{_TIMER_FILE}"),
        Subject(_FIXTURE, "files:\n  - {src: a.sh, dst: /usr/lib/homelab/a}\n"),
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
            "changes state, and ansible/roles/k8s/nut/CLAUDE.md documents it"
        ),
    },
)
