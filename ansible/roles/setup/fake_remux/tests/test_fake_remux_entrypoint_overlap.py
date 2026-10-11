"""Two "from its new home" tests repeat the moved fake_remux logic suites (#4381).

`test_fake_remux_logic.py` and `test_fake_remux_replace_logic.py` cover `encoder_is_reencoder`
and `in_size_band` in depth. The two entrypoint tests named in `DUPLICATES` re-assert them; what
they add is finding each module by the function it defines rather than by its basename. Each one
is either deleted, or carries a decision marker at it that says why it stays. A decision marker
is a line `docs/reference/decisions.py` records, so its own marker text is the oracle here.

Run: uv run pytest ansible/roles/setup/fake_remux/tests/test_fake_remux_entrypoint_overlap.py
"""

import ast

from docs.reference.decisions import _MARKER

from lib.git import git
from lib.repo_paths import REPO

ENTRYPOINT_TESTS = "ansible/roles/setup/fake_remux/tests/test_fake_remux_entrypoint.py"
DUPLICATES = frozenset(
    {
        "test_detection_logic_flags_a_reencode_from_its_new_home",
        "test_replacement_logic_applies_the_size_band_from_its_new_home",
    }
)


def _tracked_python() -> list[str]:
    out = git("ls-files", "-z", "--", "*.py", cwd=REPO, timeout=60).stdout
    return [p for p in out.split("\0") if p]


def _bodies(tree: ast.Module):
    """Each statement list a test function can sit in, with the line before its first statement."""
    yield tree.body, 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            yield node.body, node.lineno


def _unmarked_duplicates(source: str) -> list[str]:
    """The DUPLICATES defined in `source` that no decision marker sits at.

    Adjacent definitions form one run, so one marker above the pair counts for both. A run
    starts on the line after the statement before it, which takes in the comment block and the
    decorators above its first definition, and ends on the last line of its last definition.
    """
    lines = source.splitlines()
    unmarked = []
    for body, before in _bodies(ast.parse(source)):
        runs: list[tuple[int, int, list[str]]] = []
        prev_end, prev_was_duplicate = before, False
        for node in body:
            is_duplicate = isinstance(node, ast.FunctionDef) and node.name in DUPLICATES
            if is_duplicate and prev_was_duplicate:
                first, _, names = runs[-1]
                runs[-1] = (first, node.end_lineno, [*names, node.name])
            elif is_duplicate:
                runs.append((prev_end + 1, node.end_lineno, [node.name]))
            prev_end, prev_was_duplicate = node.end_lineno, is_duplicate
        for first, last, names in runs:
            if not any(
                _MARKER in line and line.split(_MARKER, 1)[1].strip()
                for line in lines[first - 1 : last]
            ):
                unmarked += names
    return unmarked


def test_duplicate_logic_tests_are_deleted_or_carry_a_decision_marker():
    scanned, unmarked = set(), {}
    for rel in _tracked_python():
        path = REPO / rel
        if not path.is_file():
            continue
        scanned.add(rel)
        text = path.read_text()
        if not any(name in text for name in DUPLICATES):
            continue
        names = _unmarked_duplicates(text)
        if names:
            unmarked[rel] = sorted(names)
    assert ENTRYPOINT_TESTS in scanned
    assert unmarked == {}
