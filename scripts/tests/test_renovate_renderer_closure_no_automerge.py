#!/usr/bin/env python3
"""Guard the Renovate rule that keeps the renderer's dependency closure off automerge.

R4 Open question 12 (option 2, ruled 2026-11-07) keeps no required render-diff context. A
renderer dependency bump that moves output is stopped only by this rule, so the rule must name
every package the renderer imports and every package those depend on in `uv.lock`, and it must
come after every rule that sets `automerge: true`, because Renovate lets a later rule win.

Run: uv run pytest scripts/tests/test_renovate_renderer_closure_no_automerge.py
"""

import ast
import subprocess
import sys
import tomllib

from _renovate import _PACKAGE_RULES
from lib.repo_paths import REPO

# An import's top-level module, where its distribution's name differs.
DISTRIBUTIONS = {"yaml": "pyyaml"}
# Where `import x` finds first-party code, as the renderer's own sys.path bootstrap does.
ROOTS = (
    REPO / "scripts",
    REPO / "scripts" / "lib",
    REPO / "scripts" / "renderer",
    REPO / "scripts" / "renderer" / "job_files",
    REPO / "scripts" / "deploy_tools",
    REPO / "ansible" / "filter_plugins",
)


def renderer_imports() -> set[str]:
    """The third-party top-level modules the renderer imports, following first-party imports."""
    listing = subprocess.run(
        ["git", "ls-files", "scripts/renderer/*.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.split()
    pending = [REPO / name for name in listing if "/tests/" not in name]
    seen, found = set(), set()
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                modules = [node.module]
            else:
                continue
            for module in modules:
                parts = module.split(".")
                local = [
                    c
                    for root in ROOTS
                    for c in (
                        root.joinpath(*parts).with_suffix(".py"),
                        root.joinpath(*parts, "__init__.py"),
                        root.joinpath(*parts),
                    )
                    if c.exists()
                ]
                if local:
                    pending += [c for c in local[:1] if c.suffix == ".py"]
                elif parts[0] not in sys.stdlib_module_names:
                    found.add(parts[0])
    return found


def lock_closure(lock: str, roots: set[str]) -> set[str]:
    """`roots` and everything they depend on, as `lock` resolves them."""
    packages = {p["name"].lower(): p for p in tomllib.loads(lock)["package"]}
    closure, pending = set(), [DISTRIBUTIONS.get(m, m) for m in roots]
    while pending:
        name = pending.pop().lower()
        if name not in closure:
            closure.add(name)
            pending += [d["name"] for d in packages[name].get("dependencies", [])]
    return closure


def closure_problems(rules: list[dict], closure: set[str]) -> list[str]:
    """What the packageRules leave open: a closure member the rule omits, or a later automerge."""
    guards = [
        i
        for i, rule in enumerate(rules)
        if rule.get("automerge") is False
        and "pypi" in rule.get("matchDatasources", [])
        and closure & set(rule.get("matchPackageNames", []))
    ]
    if not guards:
        return [
            "no automerge:false rule names a renderer dependency on the pypi datasource"
        ]
    guard = guards[-1]
    named = set(rules[guard]["matchPackageNames"])
    problems = [
        f"{name} is in the renderer's closure and the rule omits it"
        for name in sorted(closure - named)
    ]
    problems += [
        f"packageRules[{i}] sets automerge:true after the rule and may override it"
        for i, rule in enumerate(rules)
        if i > guard and rule.get("automerge") is True
    ]
    return problems


def test_the_rule_names_the_renderers_closure() -> None:
    imports = renderer_imports()
    assert imports == {"yaml", "jinja2"}, (
        "the renderer imports a package this guard has not read"
    )
    closure = lock_closure((REPO / "uv.lock").read_text(), imports)
    assert {"jinja2", "pyyaml", "markupsafe"} <= closure
    assert closure_problems(_PACKAGE_RULES, closure) == []


def test_a_rule_missing_a_closure_member_is_flagged() -> None:
    rule = {
        "matchDatasources": ["pypi"],
        "matchPackageNames": ["jinja2", "pyyaml"],
        "automerge": False,
    }
    later = {"matchManagers": ["custom.regex"], "automerge": True}
    problems = closure_problems([rule, later], {"jinja2", "pyyaml", "markupsafe"})
    assert problems == [
        "markupsafe is in the renderer's closure and the rule omits it",
        "packageRules[1] sets automerge:true after the rule and may override it",
    ]
