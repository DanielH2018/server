"""The rendered-documents census row: a renderer module reads a rendered object as a `K8sObject`.

The row sits in its own module because `test_census_rows_python.py`, which imports it into
`ROWS`, is at the cap for a test module.
"""

import ast

from _row_table import Census, Subject, tracked

# Each function still allowed its own `safe_load_all` walk, keyed by module and function, so an
# exemption for one function never covers a sibling in the same module.
OWN_WALKS = {
    ("scripts/renderer/renderer_lib/documents.py", "parse_documents"): (
        "the walk every other reader calls"
    ),
    ("scripts/renderer/renderer_lib/images.py", "paths_into_images"): (
        "reads `spec.path` and kustomization fields, never kind or metadata"
    ),
    ("scripts/renderer/renderer_lib/suspension.py", "suspends_tag"): (
        "reads the first document's `spec` and parses nothing after it"
    ),
    ("scripts/renderer/renderer_lib/suspension.py", "suspend_false"): (
        "skips a bundle file it cannot read, and `K8sObject.from_item` raises on a mapping "
        "whose `metadata` or `status` is a scalar, which would abort the render instead"
    ),
    ("scripts/renderer/renderer_lib/sops_entries.py", "sops_manifest_entries"): (
        "counts every non-null document, so a scalar beside the Secret is a problem; "
        "`parse_documents` would drop it"
    ),
    ("scripts/renderer/renderer_lib/shadow.py", "_secret_data_values"): (
        "reads every bundle file, image contexts included, and `K8sObject.from_item` "
        "raises on a mapping whose `metadata` or `status` is a scalar"
    ),
    ("scripts/renderer/renderer_lib/placeholders.py", "standalone_keys"): (
        "walks every node of every document for a placeholder, not an object's fields"
    ),
}


def _walking_functions(tree: ast.AST) -> set[str]:
    """The innermost function around each `<x>.safe_load_all(...)` call; `<module>` at top level."""
    found = set()

    def visit(node: ast.AST, function: str) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            function = node.name
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "safe_load_all"
        ):
            found.add(function)
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, "<module>")
    return found


def _own_walks(subject: Subject) -> list[str]:
    try:
        tree = ast.parse(subject.text)
    except SyntaxError:
        return []
    walking = _walking_functions(tree)
    exempt = {fn for rel, fn in OWN_WALKS if rel == subject.rel}
    hits = [
        f"{fn}() walks a YAML stream itself; read it through parse_documents"
        for fn in sorted(walking - exempt)
    ]
    hits += [
        f"OWN_WALKS names {fn}(), which no longer calls safe_load_all"
        for fn in sorted(exempt - walking)
    ]
    return hits


RENDERED_OBJECTS_ARE_PARSED = Census(
    name="renderer-reads-rendered-objects-through-parse-documents",
    reason=(
        "`renderer.renderer_lib.documents.parse_documents` reads each document of a rendered "
        "manifest into a `K8sObject`, which owns where `kind`, `name`, `namespace` and `spec` "
        "live and what a missing one reads as. A module that walks the stream itself reads "
        "those fields from the raw dict again. A walk that cannot use it goes in `OWN_WALKS` "
        "with the reason. `scripts/renderer/renderer_lib/` alone is in scope."
    ),
    files=lambda: [
        rel
        for rel in tracked("scripts/renderer/renderer_lib/*.py")
        if "/tests/" not in rel
    ],
    offence=_own_walks,
    red=(
        Subject(
            "a.py",
            "def jobs(text):\n"
            "    return [d for d in yaml_fast.safe_load_all(text) if d['kind'] == 'Job']\n",
        ),
        Subject(
            "scripts/renderer/renderer_lib/images.py",
            "def paths_into_images(text):\n    return yaml_fast.safe_load_all(text)\n"
            "def check_images(text):\n    return yaml_fast.safe_load_all(text)\n",
        ),
        Subject(
            "scripts/renderer/renderer_lib/images.py",
            "def paths_into_images(text):\n    return yaml.safe_load(text)\n",
        ),
    ),
    green=(
        Subject(
            "a.py",
            "def jobs(files):\n"
            "    return [o for o, _ in parse_documents(files) if o.kind == 'Job']\n",
        ),
        Subject(
            "scripts/renderer/renderer_lib/images.py",
            "def paths_into_images(text):\n    return yaml_fast.safe_load_all(text)\n",
        ),
    ),
    min_matches=30,
    must_find=frozenset(
        {
            "scripts/renderer/renderer_lib/documents.py",
            "scripts/renderer/renderer_lib/ordering.py",
            "scripts/renderer/renderer_lib/cronjob_gate.py",
        }
    ),
)
