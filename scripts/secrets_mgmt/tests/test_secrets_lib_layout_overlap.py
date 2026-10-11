"""`test_secrets_lib_layout.py` checks where the libraries live, not their tier rules (#4374).

`test_secret_classify.py` and `test_secret_registry.py` own the `classify()` and `due_date()`
rules. The one thing the layout file adds is finding each library by the function it defines,
so a basename change under `secrets_lib/` cannot break it. It keeps that and drops the rest.

Run: uv run pytest scripts/secrets_mgmt/tests/test_secrets_lib_layout_overlap.py
"""

import ast

from lib.git import git
from lib.repo_paths import REPO

LAYOUT = "scripts/secrets_mgmt/tests/test_secrets_lib_layout.py"
LIBRARY_FUNCTIONS = ("classify", "due_date")


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def test_layout_file_locates_each_library_but_asserts_no_tier_rule():
    tracked = git("ls-files", "--", LAYOUT, cwd=REPO, timeout=60).stdout.split()
    assert tracked == [LAYOUT], f"{LAYOUT} is not tracked; this check has no subject"
    tree = ast.parse((REPO / LAYOUT).read_text(), filename=LAYOUT)
    # The location check names each function as data, e.g. `_lib_file_defining("classify")`.
    located = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value in LIBRARY_FUNCTIONS
    }
    assert located == set(LIBRARY_FUNCTIONS), (
        "the layout file no longer finds each library by the function it defines"
    )
    rule_calls = sorted(
        f"line {node.lineno}: {_called_name(node)}()"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _called_name(node) in LIBRARY_FUNCTIONS
    )
    assert rule_calls == []
