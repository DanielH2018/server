#!/usr/bin/env python3
"""Every committed dashboard is byte-identical to what the exporter would write for it.

`export_grafana_dashboards.py` and `fetch_grafana_dashboards.py` overwrite the JSON under
`files/dashboards/` and the operator reads `git diff --stat` afterwards to see what changed
live. That read is only meaningful if a board that did NOT change produces no diff — so the
committed form has to be the writers' form exactly: keys sorted at every depth, 2-space
indent, non-ASCII literal, one trailing newline. Five boards were committed in Grafana's
own key order before #2157 and diffed on every export.

The oracle is the writer itself (`export_grafana_dashboards.dump`), not a copy of its rules.

Run: uv run pytest ansible/tests/services/test_committed_dashboards_are_a_fixed_point_of_the_exporter.py
"""

import json

import export_grafana_dashboards as eg
from _helpers import ANSIBLE

DASHBOARDS_DIR = ANSIBLE / "roles" / "k8s" / "claude-otel" / "files" / "dashboards"

# Named so a moved directory fails by name, not by an empty glob passing.
KNOWN_BOARDS = frozenset({"Infrastructure/node-exporter-full.json", "Logs/logs.json"})


def _committed() -> dict[str, str]:
    return {
        str(p.relative_to(DASHBOARDS_DIR)): p.read_text(encoding="utf-8")
        for p in sorted(DASHBOARDS_DIR.rglob("*.json"))
    }


def test_the_scan_finds_the_committed_dashboards():
    found = set(_committed())
    assert KNOWN_BOARDS <= found, sorted(KNOWN_BOARDS - found)


def test_every_committed_dashboard_is_what_the_exporter_writes():
    drifted = [
        rel for rel, text in _committed().items() if eg.dump(json.loads(text)) != text
    ]
    assert not drifted, (
        f"{drifted} are not in the exporter's form, so an export of an unchanged Grafana "
        f"rewrites them. Re-serialise with export_grafana_dashboards.dump (keys sorted, "
        f"indent=2, ensure_ascii=False, trailing newline)."
    )
