"""Doc fragments for the monitor-bridge pages (docs/monitor-bridge-checks.md, docs/monitor-bridge-internals.md).

Each entry in `FRAGMENTS` builds one fragment the way `gen_doc_fragments.py` builds its own:
a reader that parses the tree statically, and a pure renderer from what it returned to
markdown. `gen_doc_fragments.FRAGMENTS` merges this table, prepends the provenance header and
writes the file, so a builder returns only `(body, sources)`.

Three fragments, all derived from the monitor-bridge role without importing it:

- `bridge-checks`: one row per `CHECKS` entry in `check_table.py`, in registry order.
- `bridge-gate-sets`: the members of each gate set, from the `gate` column of those rows. The
  set names come from the `NAME = _members("<gate>")` assignments in `gates.py`.
- `bridge-thresholds`: the numeric and duration literals the env-secret template renders.
  A value built from a role variable (`{{ ... }}`) is skipped, so a secret never reaches a page.
"""

import ast
import re
import sys as _sys
from collections.abc import Callable
from pathlib import Path as _Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.repo_paths import FILTER_PLUGINS, MONITOR_BRIDGE_FILES, REPO, K8S_ROLES

_sys.path.insert(0, str(FILTER_PLUGINS))

from py_table import py_table

BRIDGE_ROLE = K8S_ROLES / "monitor-bridge"
CHECK_TABLE = MONITOR_BRIDGE_FILES / "check_table.py"
GATES = MONITOR_BRIDGE_FILES / "gates.py"
ENV_SECRET = BRIDGE_ROLE / "templates" / "env-secret.yaml.j2"

# The gate value that names no gate row: a reach-out check held `up` through its first down
# cycles instead of suppressed (`bridge/streaks.py:apply_startup_grace`).
STARTUP_GRACE_GATE = "startup_grace"

# An env value that is a number or a duration (`300`, `0.02`, `2h`, `30m`).
_TUNABLE_VALUE = re.compile(r"^-?\d+(?:\.\d+)?[smhd]?$")
# `  KEY: value` at the Secret's `stringData` indent; a comment or a `{% %}` line never matches.
_ENV_LINE = re.compile(r"^  ([A-Z][A-Z0-9_]*): (.*)$")
# Process settings, not thresholds.
_NOT_TUNABLE_PREFIXES = ("PYTHON",)
_NOT_TUNABLE_KEYS = frozenset({"TZ"})


def _rel(path: _Path) -> str:
    return path.relative_to(REPO).as_posix()


# --- readers --------------------------------------------------------------------------------


def read_check_rows(source: str) -> list[dict]:
    """The literal keyword arguments of every `PushCheck` in `CHECKS`, in registry order."""
    return py_table(source, "CHECKS")


def read_gate_set_names(source: str) -> dict[str, str]:
    """gate value -> the set name `gates.py` derives from it.

    Reads the module-level `NAME = _members("<gate>")` assignments, so a set renamed or added
    in `gates.py` changes the fragment without a second list here.
    """
    names = {}
    for node in ast.parse(source).body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target, call = node.targets[0], node.value
        if (
            isinstance(target, ast.Name)
            and isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_members"
            and len(call.args) == 1
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
        ):
            names[call.args[0].value] = target.id
    return names


def read_env_tunables(template: str) -> list[tuple[str, str]]:
    """(key, value) for each numeric or duration literal the env-secret renders, in order."""
    tunables = []
    for line in template.splitlines():
        match = _ENV_LINE.match(line)
        if not match:
            continue
        key, raw = match.groups()
        value = raw.strip().strip("\"'")
        if (
            "{{" in raw
            or key in _NOT_TUNABLE_KEYS
            or key.startswith(_NOT_TUNABLE_PREFIXES)
            or not _TUNABLE_VALUE.match(value)
        ):
            continue
        tunables.append((key, value))
    return tunables


# --- renderers ------------------------------------------------------------------------------


def render_bridge_checks(rows: list[dict]) -> str:
    """Renders the registry table: one line per check or gate, in `CHECKS` order.

    Args:
        rows: the keyword-literal dicts `read_check_rows` returns.
    """
    lines = [
        "| Tile | Check | Gate | Critical | Status page group | Runbook |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        if row.get("is_gate"):
            gate = "is a gate"
        else:
            gate = f"`{row['gate']}`" if row.get("gate") else "none"
        runbook = f"`{row['runbook']}`" if row.get("runbook") else ""
        lines.append(
            f"| {row['display']} | `{row['name']}` | {gate} "
            f"| {'yes' if row.get('critical') else ''} | {row['status_group']} | {runbook} |"
        )
    return "\n".join(lines) + "\n"


def render_gate_sets(rows: list[dict], set_names: dict[str, str]) -> str:
    """Renders the gate-set membership table from the `gate` column.

    Args:
        rows: the keyword-literal dicts `read_check_rows` returns.
        set_names: gate value -> set name, from `read_gate_set_names`.
    """
    displays = {row["name"]: row["display"] for row in rows if row.get("is_gate")}
    members: dict[str | None, list[str]] = {}
    for row in rows:
        if not row.get("is_gate"):
            members.setdefault(row.get("gate"), []).append(f"`{row['name']}`")

    def line(label: str, held_by: str, names: list[str]) -> str:
        return f"| {label} | {held_by} | {len(names)} | {', '.join(names)} |"

    lines = [
        "| Set | Held by | Members | Checks |",
        "|---|---|---|---|",
    ]
    for gate, display in displays.items():
        lines.append(
            line(
                f"`{set_names.get(gate, gate)}`",
                f"{display} gate, which suppresses them",
                members.get(gate, []),
            )
        )
    lines.append(
        line(
            f"`{set_names.get(STARTUP_GRACE_GATE, STARTUP_GRACE_GATE)}`",
            "startup grace, which holds them `up` through their first down cycles",
            members.get(STARTUP_GRACE_GATE, []),
        )
    )
    lines.append(line("ungated", "nothing", members.get(None, [])))
    return "\n".join(lines) + "\n"


def render_bridge_thresholds(tunables: list[tuple[str, str]]) -> str:
    """Renders the threshold table: env key -> the value the env-secret template renders.

    Args:
        tunables: (key, value) pairs from `read_env_tunables`, in template order.
    """
    lines = ["| Env key | Value |", "|---|---|"]
    lines.extend(f"| `{key}` | `{value}` |" for key, value in tunables)
    return "\n".join(lines) + "\n"


# --- builders -------------------------------------------------------------------------------


def _bridge_checks() -> tuple[str, list[str]]:
    rows = read_check_rows(CHECK_TABLE.read_text())
    return render_bridge_checks(rows), [_rel(CHECK_TABLE)]


def _bridge_gate_sets() -> tuple[str, list[str]]:
    rows = read_check_rows(CHECK_TABLE.read_text())
    names = read_gate_set_names(GATES.read_text())
    return render_gate_sets(rows, names), [_rel(CHECK_TABLE), _rel(GATES)]


def _bridge_thresholds() -> tuple[str, list[str]]:
    tunables = read_env_tunables(ENV_SECRET.read_text())
    return render_bridge_thresholds(tunables), [_rel(ENV_SECRET)]


# name -> () -> (body, sources). The name is the file stem a page includes.
FRAGMENTS: dict[str, Callable[[], tuple[str, list[str]]]] = {
    "bridge-checks": _bridge_checks,
    "bridge-gate-sets": _bridge_gate_sets,
    "bridge-thresholds": _bridge_thresholds,
}
