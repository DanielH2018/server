"""`_as_filter` has one home, and every filter plugin that wraps its filters imports it (#4527).

`_as_filter` re-raises a filter's `ValueError` as `AnsibleFilterError("<filter>: ...")`. After
#4480 and #4505 seven plugins carried their own verbatim copy, so a change to its contract had
to be made seven times and nothing checked the copies agreed.

Each plugin's `filters()` stays a literal `{"name": _as_filter(fn)}` dict, because
`scripts/deploy_tools/narrow_lib/filters.py:filter_names` reads the filter names statically.

Run: uv run pytest ansible/tests/repo/test_filter_plugins_share_as_filter.py
"""

import ast
import importlib

from _helpers import REPO
from _row_table import tracked

WRAPPER = "_as_filter"

# The plugins that wrapped their filters in a local copy when #4527 was filed.
KNOWN_WRAPPING_PLUGINS = frozenset(
    {
        "homepage_tiles",
        "kuma_monitors",
        "longhorn_groups",
        "py_table",
        "role_label",
        "scrape_jobs",
        "service_tier",
    }
)


def _plugin_rel(name: str) -> str:
    return f"ansible/filter_plugins/{name}.py"


def _defines_wrapper(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.FunctionDef) and node.name == WRAPPER
        for node in ast.walk(tree)
    )


def _filters_dict(tree: ast.Module) -> ast.Dict | None:
    """The literal dict `FilterModule.filters()` returns, or None when it returns no dict."""
    for cls in tree.body:
        if isinstance(cls, ast.ClassDef) and cls.name == "FilterModule":
            for fn in cls.body:
                if isinstance(fn, ast.FunctionDef) and fn.name == "filters":
                    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
                    if len(returns) == 1 and isinstance(returns[0].value, ast.Dict):
                        return returns[0].value
    return None


def test_as_filter_is_defined_at_most_once_in_the_tracked_code():
    """The Verify-by: `def _as_filter` sums to at most 1, wherever its one home is."""
    scanned = [
        rel
        for rel in tracked("ansible/*.py", "scripts/*.py")
        if "/tests/" not in rel and not rel.startswith("ansible/collections/")
    ]
    assert {_plugin_rel(name) for name in KNOWN_WRAPPING_PLUGINS} <= set(scanned)
    defining = sorted(
        rel for rel in scanned if _defines_wrapper(ast.parse((REPO / rel).read_text()))
    )
    assert len(defining) <= 1, (
        f"`def {WRAPPER}` appears in {len(defining)} modules: {defining}"
    )


def test_each_wrapping_plugin_imports_as_filter_and_keeps_a_literal_filters_dict():
    filter_names = importlib.import_module("narrow_lib.filters").filter_names
    problems = []
    for name in sorted(KNOWN_WRAPPING_PLUGINS):
        rel = _plugin_rel(name)
        text = (REPO / rel).read_text()
        tree = ast.parse(text)
        if _defines_wrapper(tree):
            problems.append(f"{rel} defines its own `{WRAPPER}`")
        returned = _filters_dict(tree)
        if returned is None:
            problems.append(f"{rel}: filters() returns no literal dict")
            continue
        unwrapped = [
            ast.unparse(value)
            for value in returned.values
            if not (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == WRAPPER
            )
        ]
        if unwrapped:
            problems.append(
                f"{rel}: filters() values not wrapped in `{WRAPPER}`: {unwrapped}"
            )
        keys = {k.value for k in returned.keys if isinstance(k, ast.Constant)}
        if filter_names(text, rel) != keys:
            problems.append(
                f"{rel}: the narrowing census no longer reads its filter names"
            )
    assert not problems, "\n".join(problems)


def test_every_wrapping_plugin_runs_the_same_wrapper():
    """One home means one function: every wrapped filter shares the wrapper's code object."""
    wrapper_codes = {}
    for name in sorted(KNOWN_WRAPPING_PLUGINS):
        filters = importlib.import_module(name).FilterModule().filters()
        assert len(filters) >= 1, f"{name} registers no filters"
        for filter_name, fn in filters.items():
            wrapper_codes.setdefault(fn.__code__, []).append(f"{name}.{filter_name}")
    assert len(wrapper_codes) == 1, (
        f"{len(wrapper_codes)} distinct wrappers: "
        + "; ".join(sorted(", ".join(users[:2]) for users in wrapper_codes.values()))
    )
