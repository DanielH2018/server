"""Two census rows over what a role ships under `files/`.

`test_census_rows_roles.py` runs them with its own rows, from a module of their own so that
file stays under its module-length cap.
"""

import json
from collections import defaultdict
from pathlib import PurePosixPath

import export_grafana_dashboards as exporter
from _row_table import Census, Subject, tracked

_DASHBOARDS = "k8s/roles/observability/files/dashboards"


def _roles_shipping_python() -> list[Subject]:
    """One subject per role that ships a `files/**/*.py`, its text the role's tracked tests."""
    tests = defaultdict(list)
    for rel in tracked("k8s/roles/*/tests/*.py"):
        tests[PurePosixPath(rel).parts[2]].append(rel)
    roles = sorted(
        {PurePosixPath(rel).parts[2] for rel in tracked("k8s/roles/*/files/*.py")}
    )
    return [Subject(f"k8s/roles/{role}", "\n".join(tests[role])) for role in roles]


def _dashboard_offence(subject: Subject) -> list[str]:
    if exporter.dump(json.loads(subject.text)) == subject.text:
        return []
    return ["is not in the form export_grafana_dashboards.dump writes"]


ROLE_PYTHON_HAS_TESTS = Census(
    name="role-shipping-python-has-tests",
    reason=(
        "A role that ships a `files/*.py` with logic gets a `tests/` directory "
        "(.claude/rules/python-layout.md). The sibling guards start from test files, so a "
        "role with code and no tests at all is invisible to them. "
        "`tests/repo/test_testpaths_covers_every_test_file.py` holds each test it finds "
        "under a testpath."
    ),
    files=_roles_shipping_python,
    offence=lambda s: (
        [] if s.text else ["ships files/**/*.py and tracks no tests/*.py"]
    ),
    red=(Subject("k8s/roles/newthing", ""),),
    green=(Subject("k8s/roles/thing", "k8s/roles/thing/tests/test_mod.py"),),
    min_matches=25,
    must_find=frozenset(
        f"k8s/roles/{role}" for role in ("monitor-bridge", "karakeep", "ical-proxy")
    ),
)

DASHBOARDS_IN_EXPORTER_FORM = Census(
    name="committed-dashboards-are-in-the-exporters-form",
    reason=(
        "export_grafana_dashboards.py and fetch_grafana_dashboards.py overwrite the JSON "
        "under files/dashboards/, and the operator reads `git diff --stat` afterwards to see "
        "what changed live. That read means something only if a board that did not change "
        "produces no diff, so the committed form is the writer's: keys sorted at every "
        "depth, a 2-space indent, non-ASCII literal and one trailing newline. Re-serialise "
        "with export_grafana_dashboards.dump."
    ),
    files=lambda: tracked(f"{_DASHBOARDS}/*.json"),
    offence=_dashboard_offence,
    red=(Subject("a.json", '{"b": 1, "a": 2}\n'), Subject("a.json", '{"a": 1}')),
    green=(Subject("a.json", exporter.dump({"b": 1, "a": {"d": "é", "c": 2}})),),
    min_matches=20,
    must_find=frozenset(
        {
            f"{_DASHBOARDS}/Infrastructure/node-exporter-full.json",
            f"{_DASHBOARDS}/Logs/logs.json",
        }
    ),
)
