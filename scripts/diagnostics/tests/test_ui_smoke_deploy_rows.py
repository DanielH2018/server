"""The deploy UI smoke test's expected row count agrees with the page it drives (#7853).

`test_ui_smoke.py` is `-m ui`, which CI does not run, so its `DEPLOYER_STATE_ROWS` went stale
unnoticed. These run in CI and read the page source, the oracle the smoke test cannot supply.
"""

import ast
import re

from lib.repo_paths import REPO

SMOKE = REPO / "scripts/diagnostics/tests/test_ui_smoke.py"
PAGE = REPO / "hosts/components/deploy-ui/files/deploy_ui.html"
READS = REPO / "hosts/components/deploy-ui/files/deploy_ui_lib/deploy_ui_reads.py"


def _state_return_expression() -> str:
    body = PAGE.read_text().split("function state(", 1)[1].split("\nfunction ", 1)[0]
    return body.split("return ", 1)[1]


def _rendered_row_names() -> list[str]:
    expr = _state_return_expression()
    helper_rows = re.findall(r"\brow\('([a-z_]+)'", expr)
    literal_rows = re.findall(r"<tr><td>([a-z_]+)</td>", expr)
    return helper_rows + literal_rows


def _smoke_constant(name: str) -> int:
    for node in ast.parse(SMOKE.read_text()).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{SMOKE.name} defines no {name}")


def test_smoke_row_count_matches_the_rows_state_renders():
    rendered = _rendered_row_names()
    assert "hold_plane" in rendered, f"row parse found only {rendered}"
    expected = _smoke_constant("DEPLOYER_STATE_ROWS")
    assert expected == len(rendered), (
        f"DEPLOYER_STATE_ROWS is {expected}, but state() in {PAGE.name} renders "
        f"{len(rendered)} rows: {rendered}"
    )


def test_smoke_cites_only_names_deploy_ui_reads_defines():
    defined = set()
    for node in ast.parse(READS.read_text()).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            defined.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
    cited = set(re.findall(r"deploy_ui_reads\.([A-Za-z_]\w*)", SMOKE.read_text()))
    assert cited <= defined, (
        f"{SMOKE.name} cites missing names: {sorted(cited - defined)}"
    )
