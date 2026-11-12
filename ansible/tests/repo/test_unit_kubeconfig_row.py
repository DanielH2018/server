"""A non-root unit whose `ExecStart` reaches the cluster sets `KUBECONFIG` (#8155).

Without it, k3s's kubectl falls back to the root-only `/etc/rancher/k3s/k3s.yaml`, reads an
empty cluster and exits 0. The rule is the `Census` row `UNITS_REACHING_THE_CLUSTER_SET_KUBECONFIG`
in `ansible/tests/repo/_unit_kubeconfig_row.py`, wired into a census rows table. It selects
`hosts/components/*/units/*.service*` and the claude-code unit templates.

Each red below is a real unit with its `KUBECONFIG` assignment stripped, kept at its real path
so the row can resolve the wrapper through the component's `component.yml` file map or follow
the repo script it runs.

Run: uv run pytest ansible/tests/repo/test_unit_kubeconfig_row.py
"""

import importlib
import importlib.util
import re

from _helpers import REPO
from _row_table import Census, Subject, check, proof_problems

ROW_MODULE = "_unit_kubeconfig_row"
ROW_NAME = "UNITS_REACHING_THE_CLUSTER_SET_KUBECONFIG"

INFRA_MAP = "hosts/components/infra-map/units/homelab-infra-map.service.j2"
DOCS_REFRESH = "hosts/components/docs-refresh/units/homelab-docs-refresh.service.j2"
B2_BUDGET = (
    "hosts/components/k3s-host-checks/units/homelab-b2-budget-listing.service.j2"
)
B2_DELETIONS = (
    "hosts/components/k3s-host-checks/units/homelab-b2-deletion-accounting.service.j2"
)
CLAUDE_RC = "hosts/components/claude-code/templates/claude-rc.service.j2"
BG_RESUME = "hosts/components/claude-code/templates/claude-bg-resume.service.j2"
TRIAGE = "hosts/components/claude-code/templates/claude-triage@.service.j2"
MEMORY_SYNC = "hosts/components/claude-code/templates/claude-memory-sync.service.j2"

KUBECONFIG_UNITS = (
    INFRA_MAP,
    DOCS_REFRESH,
    B2_BUDGET,
    B2_DELETIONS,
    CLAUDE_RC,
    BG_RESUME,
    TRIAGE,
)


def _row() -> Census:
    assert importlib.util.find_spec(ROW_MODULE), (
        f"no {ROW_MODULE}: the #8155 row does not exist"
    )
    return getattr(importlib.import_module(ROW_MODULE), ROW_NAME)


def _text(rel: str) -> str:
    return (REPO / rel).read_text()


def _without_kubeconfig(rel: str) -> str:
    """The unit's text with every `KUBECONFIG=` assignment dropped and the rest kept."""
    text = re.sub(r"^Environment=KUBECONFIG=.*\n", "", _text(rel), flags=re.M)
    text = re.sub(r" KUBECONFIG=(?:\{\{.*?\}\}|[^\s{])*", "", text)
    assert "KUBECONFIG=" not in text, f"{rel}: the fixture still sets KUBECONFIG"
    return text


def _as_user(text: str, user_line: str) -> str:
    replaced, n = re.subn(r"^User=.*\n", user_line, text, count=1, flags=re.M)
    assert n == 1, "the fixture unit has no User= line to replace"
    return replaced


def test_the_row_holds_on_the_tree_with_its_red_and_green_proof():
    row = _row()
    assert not check(row), "\n".join(check(row))
    assert not proof_problems(row), proof_problems(row)


def test_the_row_names_every_unit_that_sets_kubeconfig_as_must_find():
    assert set(KUBECONFIG_UNITS) <= _row().must_find


def test_a_wrapper_resolved_through_the_component_file_map_without_kubeconfig_is_flagged():
    unit = _without_kubeconfig(DOCS_REFRESH)
    assert "ExecStart=/usr/lib/homelab/docs-refresh\n" in unit
    assert _row().offence(Subject(DOCS_REFRESH, unit))


def test_a_repo_script_reaching_kubectl_through_lib_kubectl_without_kubeconfig_is_flagged():
    unit = _without_kubeconfig(B2_BUDGET)
    assert "scripts/diagnostics/probe.py" in unit
    assert "Environment=PATH=" in unit
    assert _row().offence(Subject(B2_BUDGET, unit))


def test_a_claude_code_unit_template_without_kubeconfig_is_flagged():
    assert _row().offence(Subject(CLAUDE_RC, _without_kubeconfig(CLAUDE_RC)))


def test_kubeconfig_on_a_shared_environment_line_counts_as_set():
    unit = _text(B2_BUDGET)
    assert "Environment=PATH=" in unit and " KUBECONFIG=" in unit
    assert not _row().offence(Subject(B2_BUDGET, unit))


def test_a_unit_running_as_root_needs_no_kubeconfig():
    row = _row()
    stripped = _without_kubeconfig(B2_BUDGET)
    assert row.offence(Subject(B2_BUDGET, stripped))
    assert not row.offence(Subject(B2_BUDGET, _as_user(stripped, "User=root\n")))
    assert not row.offence(Subject(B2_BUDGET, _as_user(stripped, "")))


def test_a_non_root_unit_that_never_reaches_the_cluster_is_not_flagged():
    unit = _text(MEMORY_SYNC)
    assert re.search(r"^User=\{\{", unit, re.M) and "KUBECONFIG" not in unit
    assert not _row().offence(Subject(MEMORY_SYNC, unit))
