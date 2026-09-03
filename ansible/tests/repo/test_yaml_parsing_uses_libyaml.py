"""First-party Python parses YAML through `lib.yaml_fast`, not `yaml.safe_load`.

PyYAML's `safe_load` uses a parser written in Python. libyaml implements the same YAML 1.1
safe schema an order of magnitude faster — measured 2026-09-03 over the 184
`ansible/**/tasks/*.yml` files, 0.42s against 0.04s — and this repo parses a lot of YAML.
Converting every call site took the full test suite from 44.9s to 30.6s at `-n 4`, and the
CI `pytest` job from 285s to 104s (PRs #1028, #1026).

WHY THIS IS A TEST AND NOT A NOTE IN CLAUDE.md. Nothing about `yaml.safe_load` looks wrong.
A new file that reaches for it parses correctly, passes review, and silently hands back a
slice of that saving — and the loss is invisible because it shows up as the suite being a bit
slower than last month, never as a failure. That is the repo's escalation ladder applied
(CLAUDE.md, "Review & Memory Hygiene"): a convention a machine enforces beats a paragraph an
agent has to remember.

WHY AST AND NOT GREP. `yaml.safe_load` appears in dozens of docstrings and comments across
`scripts/docs/` and `ansible/tests/`, describing what a generator does. A textual check would
flag every one, and the fix for that noise would be to weaken the pattern until it stopped
catching real calls. Walking the AST separates a call from a mention exactly.
"""

from __future__ import annotations

import ast
from pathlib import Path

from _helpers import REPO

# Where the convention applies. `ansible/roles/*/files/` is deliberately absent: a role ships
# only its own `files/` directory, so `scripts/lib` is unreachable from there by construction,
# and those modules run cluster-side where they are not a CI cost.
_SCANNED = ("scripts", "ansible/tests")

_BANNED = frozenset({"safe_load", "safe_load_all"})

# Call sites that stay on PyYAML's own parser, each for a stated reason. Keep this small; a
# growing list means the convention is not worth having.
_ALLOWED = frozenset(
    {
        # The equivalence this whole swap rests on is asserted by comparing the two parsers,
        # so this file must keep calling the slow one.
        "scripts/lib/tests/test_yaml_fast.py",
        # Function-local `import yaml`, one call each, on paths that run once rather than per
        # test. Converting them would mean adding a module-level import to a cold path for no
        # measurable gain.
        "scripts/diagnostics/probe_lib/health.py",
        "ansible/tests/setup/test_host_python_invocations.py",
        "ansible/tests/setup/test_github_ruleset_drift.py",
    }
)

# A file that must be found parsing YAML through the helper. Without this the census below
# passes vacuously the moment the scan roots move or a glob stops matching — nine guards broke
# exactly that way in six consecutive PRs (CLAUDE.md, "A check that finds its own subject by
# pattern ships with a named member it must find").
_MUST_USE_YAML_FAST = frozenset(
    {
        "ansible/tests/_k8s_render.py",
        "scripts/lib/invocation_sites.py",
        "scripts/validate/k8s_manifests.py",
        "scripts/validate/compose_templates.py",
    }
)


def _python_files() -> list[Path]:
    files: list[Path] = []
    for root in _SCANNED:
        files.extend(sorted((REPO / root).rglob("*.py")))
    return [f for f in files if "__pycache__" not in f.parts]


def _calls_pyyaml_directly(tree: ast.AST) -> bool:
    """True when the module CALLS `yaml.safe_load`/`safe_load_all`, mention aside."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in _BANNED
            and isinstance(func.value, ast.Name)
            and func.value.id == "yaml"
        ):
            return True
    return False


def test_no_new_call_site_uses_pyyamls_own_parser():
    offenders = []
    for path in _python_files():
        rel = path.relative_to(REPO).as_posix()
        if rel in _ALLOWED:
            continue
        if _calls_pyyaml_directly(ast.parse(path.read_text())):
            offenders.append(rel)

    assert not offenders, (
        "these call yaml.safe_load[_all] directly instead of lib.yaml_fast, which parses the "
        "same schema ~10x faster:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse `from lib import yaml_fast` and `yaml_fast.safe_load(...)`. If the call "
        "genuinely must use PyYAML's own parser, add it to _ALLOWED with the reason."
    )


def test_the_scan_actually_reaches_the_files_it_claims_to():
    """Non-vacuity: the census must find the modules that do parse YAML through the helper.

    `test_no_new_call_site_uses_pyyamls_own_parser` passes on an EMPTY file list, so on its own
    it cannot tell "nothing violates the convention" from "the scan roots moved".
    """
    scanned = {p.relative_to(REPO).as_posix() for p in _python_files()}
    assert len(scanned) > 200, (
        f"only {len(scanned)} python files scanned — roots moved?"
    )

    users = {
        rel
        for rel in scanned
        if "yaml_fast" in (REPO / rel).read_text() and not rel.endswith("yaml_fast.py")
    }
    missing = _MUST_USE_YAML_FAST - users
    assert not missing, (
        f"expected these to parse YAML through yaml_fast, found none in: {missing}"
    )


def test_a_direct_call_is_detected_and_a_mention_is_not():
    """The rejecting half, paired with the accepting one.

    A detector that fires on everything and one that fires on nothing are indistinguishable
    from the passing side, so both inputs are asserted here.
    """
    flagged = ast.parse("import yaml\ndata = yaml.safe_load(text)\n")
    assert _calls_pyyaml_directly(flagged)

    flagged_all = ast.parse("import yaml\ndocs = list(yaml.safe_load_all(text))\n")
    assert _calls_pyyaml_directly(flagged_all)

    # A docstring naming the function, which is how it appears across scripts/docs/.
    mention = ast.parse('"""Role defaults are read with yaml.safe_load."""\n')
    assert not _calls_pyyaml_directly(mention)

    # The helper's own call, which goes through yaml.load with an explicit Loader.
    helper = ast.parse("import yaml\nyaml.load(stream, Loader=yaml.CSafeLoader)\n")
    assert not _calls_pyyaml_directly(helper)

    # A local variable that happens to be named safe_load is not a yaml attribute call.
    unrelated = ast.parse("safe_load = other.safe_load\nsafe_load(text)\n")
    assert not _calls_pyyaml_directly(unrelated)
