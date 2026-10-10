"""Tests for the monitor-bridge doc fragments and the two pages that transclude them.

The renderers are pure, so they run against literals. The doc-coverage tests (a *Live checks*
bullet per `CHECKS` row, a module-table row per `files/*.py`) each have a red/green pair on
synthetic text and a run on the real page with a non-vacuity floor.
"""

import re

import fragments_bridge as fb
from lib.repo_paths import REPO

CHECKS_PAGE = REPO / "docs" / "monitor-bridge-checks.md"
INTERNALS_PAGE = REPO / "docs" / "monitor-bridge-internals.md"

# --- renderers, against literals ------------------------------------------------------------

ROWS = [
    {
        "name": "disk",
        "display": "Root Disk",
        "status_group": "Hosts & Power",
        "gate": "prometheus",
        "critical": True,
    },
    {
        "name": "n8n",
        "display": "n8n Prod Workflows",
        "status_group": "Services",
        "gate": "startup_grace",
    },
    {
        "name": "etcd_restore_drill",
        "display": "etcd Restore Drill",
        "status_group": "Backups & Storage",
        "runbook": "k3s-etcd-restore",
    },
    {
        "name": "prometheus",
        "display": "Prometheus Reachable",
        "status_group": "Observability",
        "is_gate": True,
    },
]


def test_bridge_checks_renders_one_row_per_check_in_order():
    assert fb.render_bridge_checks(ROWS).splitlines() == [
        "| Tile | Check | Gate | Critical | Status page group | Runbook |",
        "|---|---|---|---|---|---|",
        "| Root Disk | `disk` | `prometheus` | yes | Hosts & Power |  |",
        "| n8n Prod Workflows | `n8n` | `startup_grace` |  | Services |  |",
        "| etcd Restore Drill | `etcd_restore_drill` | none |  | Backups & Storage "
        "| `k3s-etcd-restore` |",
        "| Prometheus Reachable | `prometheus` | is a gate |  | Observability |  |",
    ]


def test_gate_sets_group_members_by_the_gate_column():
    names = {"prometheus": "PROM_DEPENDENT", "startup_grace": "STARTUP_GRACE"}
    assert fb.render_gate_sets(ROWS, names).splitlines() == [
        "| Set | Held by | Members | Checks |",
        "|---|---|---|---|",
        "| `PROM_DEPENDENT` | Prometheus Reachable gate, which suppresses them | 1 | `disk` |",
        "| `STARTUP_GRACE` | startup grace, which holds them `up` through their first "
        "down cycles | 1 | `n8n` |",
        "| ungated | nothing | 1 | `etcd_restore_drill` |",
    ]


def test_thresholds_renders_key_and_value():
    assert fb.render_bridge_thresholds([("DISK_MAX_PCT", "90")]).splitlines() == [
        "| Env key | Value |",
        "|---|---|",
        "| `DISK_MAX_PCT` | `90` |",
    ]


# --- readers: what counts as a tunable ------------------------------------------------------

TEMPLATE = """\
stringData:
  TZ: "{{ tz }}"
  PYTHONUNBUFFERED: "1"
  INTERVAL: "300"
  # DISK_COMMENTED: "5"
  TRAEFIK_421_RPS: "0.02"
  N8N_FAIL_WINDOW: 2h
  DISK_MOUNTPOINTS: /,/boot
  LOG_ERROR_MAX: "{{ role_var }}"
  N8N_API_KEY: "{{ n8n_api_key }}"
{% for c in checks %}
  KUMA_PUSH_{{ c }}: "{{ token }}"
{% endfor %}
"""


def test_tunables_keep_numbers_and_durations_in_template_order():
    assert fb.read_env_tunables(TEMPLATE) == [
        ("INTERVAL", "300"),
        ("TRAEFIK_421_RPS", "0.02"),
        ("N8N_FAIL_WINDOW", "2h"),
    ]


def test_tunables_never_carry_a_value_built_from_a_variable():
    keys = {key for key, _ in fb.read_env_tunables(TEMPLATE)}
    assert not keys & {"N8N_API_KEY", "LOG_ERROR_MAX", "TZ"}


def test_gate_set_names_come_from_the_members_assignments():
    source = 'PROM_DEPENDENT = _members("prometheus")\nOTHER = frozenset()\n'
    assert fb.read_gate_set_names(source) == {"prometheus": "PROM_DEPENDENT"}


# --- the fragments against the real tree ----------------------------------------------------


def test_every_check_row_reaches_the_checks_fragment():
    rows = fb.read_check_rows(fb.CHECK_TABLE.read_text())
    body = fb.render_bridge_checks(rows)
    assert len(rows) >= 40
    assert all(f"`{row['name']}`" in body for row in rows)


def test_every_non_gate_check_is_in_exactly_one_gate_set_row():
    rows = fb.read_check_rows(fb.CHECK_TABLE.read_text())
    names = fb.read_gate_set_names(fb.GATES.read_text())
    body = fb.render_gate_sets(rows, names)
    assert {"prometheus", "loki_reachable", "b2_reachable", "wan_reachable"} <= set(
        names
    )
    for row in rows:
        if not row.get("is_gate"):
            assert body.count(f"`{row['name']}`") == 1, row["name"]


def test_thresholds_fragment_finds_the_template_tunables():
    found = dict(fb.read_env_tunables(fb.ENV_SECRET.read_text()))
    assert len(found) >= 40
    assert found["INTERVAL"] == "300"
    assert found["HWMON_TEMP_CONSECUTIVE"] == "12"


# --- *Live checks* titles against CHECKS ----------------------------------------------------

_TITLE = re.compile(r"^- \*\*(.+?)\*\*(.*)$")
_ARM = "not a monitor of its own"


def live_check_titles(page: str) -> list[str]:
    """Titles of the top-level bullets in *Live checks* that name a monitor.

    A bullet that says it is "not a monitor of its own" documents an arm inside another check,
    so it names no tile.
    """
    section = page.split("\n## Live checks\n", 1)[1].split("\n## ", 1)[0]
    titles = []
    for line in section.splitlines():
        match = _TITLE.match(line)
        if match and _ARM not in match.group(2):
            titles.append(match.group(1))
    return titles


def title_drift(page: str, displays: set[str]) -> tuple[set[str], set[str]]:
    """(display names with no bullet, bullet titles naming no display name)."""
    titles = set(live_check_titles(page))
    return displays - titles, titles - displays


PAGE = """\
# page

## Live checks

- **Root Disk** (a mountpoint)
- **Host coverage floor** — not a monitor of its own, but an arm inside **Root Disk**.
  - **Nested** (an indented bullet is not a title)
- **Gone Check** (a check that has retired)

## Retired and moved checks

- **Old Check** — retired
"""


def test_a_display_name_without_a_bullet_is_reported():
    missing, _ = title_drift(PAGE, {"Root Disk", "Memory"})
    assert missing == {"Memory"}


def test_a_bullet_for_a_check_absent_from_checks_is_reported():
    _, extra = title_drift(PAGE, {"Root Disk"})
    assert extra == {"Gone Check"}


def test_a_page_that_matches_checks_is_clean():
    assert title_drift(PAGE, {"Root Disk", "Gone Check"}) == (set(), set())


def test_the_live_checks_section_stops_at_the_next_heading():
    assert "Old Check" not in live_check_titles(PAGE)


def test_every_live_check_has_a_bullet_and_every_bullet_a_live_check():
    rows = fb.read_check_rows(fb.CHECK_TABLE.read_text())
    displays = {row["display"] for row in rows}
    page = CHECKS_PAGE.read_text()
    titles = live_check_titles(page)
    assert len(titles) >= 40
    assert {"etcd Restore Drill", "B2 Free Tier Headroom"} <= set(titles)
    assert title_drift(page, displays) == (set(), set())


# --- the internals module table against files/ ----------------------------------------------

_CODE_PY = re.compile(r"`([\w.]+\.py)`")


def module_table(page: str) -> list[str]:
    """The rows of *The module table*, each as its raw line."""
    section = page.split("\n## The module table\n", 1)[1].split("\n## ", 1)[0]
    return [line for line in section.splitlines() if line.startswith("| `")]


def undocumented_modules(rows: list[str], modules: set[str]) -> set[str]:
    """Top-level module file names that no row's first cell names."""
    named = set()
    for row in rows:
        first_cell = row.split("|")[1]
        named.update(_CODE_PY.findall(first_cell))
    return modules - named


def unlisted_check_domains(rows: list[str], stems: set[str]) -> set[str]:
    """`checks/` module stems the `checks/<domain>.py` row does not name in backticks."""
    row = next(r for r in rows if r.startswith("| `checks/<domain>.py`"))
    return {stem for stem in stems if f"`{stem}`" not in row}


TABLE_PAGE = """\

## The module table

| module | holds |
|---|---|
| `cli.py` | front end |
| `a.py`, `b.py` | two modules in one row |
| `checks/<domain>.py` | bodies by domain: `service`, `host` (+ `host_edge`) |

## Next
"""


def test_a_module_without_a_row_is_reported():
    rows = module_table(TABLE_PAGE)
    assert undocumented_modules(rows, {"cli.py", "a.py", "c.py"}) == {"c.py"}


def test_modules_named_in_one_shared_row_count_as_documented():
    rows = module_table(TABLE_PAGE)
    assert undocumented_modules(rows, {"cli.py", "a.py", "b.py"}) == set()


def test_a_check_domain_missing_from_the_checks_row_is_reported():
    rows = module_table(TABLE_PAGE)
    assert unlisted_check_domains(rows, {"service", "host", "host_edge", "wan"}) == {
        "wan"
    }


def test_every_top_level_module_has_a_row_in_the_module_table():
    modules = {p.name for p in fb.MONITOR_BRIDGE_FILES.glob("*.py")} - {"__init__.py"}
    rows = module_table(INTERNALS_PAGE.read_text())
    assert {"cli.py", "check_table.py", "gitops_hold.py"} <= modules
    assert undocumented_modules(rows, modules) == set()


def test_every_checks_module_is_named_in_the_checks_row():
    stems = {p.stem for p in (fb.MONITOR_BRIDGE_FILES / "checks").glob("*.py")} - {
        "__init__"
    }
    rows = module_table(INTERNALS_PAGE.read_text())
    assert {"service", "wan", "cluster_etcd"} <= stems
    assert unlisted_check_domains(rows, stems) == set()
