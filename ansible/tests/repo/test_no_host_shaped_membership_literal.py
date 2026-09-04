"""No tracked Python file tests a host-shaped string literal with `in` / `not in`.

CodeQL's `py/incomplete-url-substring-sanitization` fires on `"<host>.<tld>" in x` wherever it
sees a hostname-shaped literal on the left of a containment test. It cannot tell a *substring*
check on a URL — the real defect the rule hunts, where `"example.com" in url` also matches
`example.com.attacker.net` — from an *exact membership* check on a list, set or dict, which is
what this repo writes every time. Fifteen alerts in this repo have been dismissed as false
positives on that rule, and two more (#49, #50) opened on 2026-09-04 against the reap-orphan
entry-point tests. Those two shapes now sit in
`ansible/roles/setup/k3s/tests/test_longhorn_reap_backups_cli.py` and
`ansible/roles/setup/k3s/tests/test_longhorn_reap_snapshots_cli.py`, which the suite was split
into. Every one was membership in a collection, several of them argv lists from a stubbed
`kubectl`.

Dismissing each alert by hand does not stop the next one, and a dismissal is re-raised when the
surrounding lines move. Writing the comparison as `any(x == "<host>" for x in seq)` states the
exact-match intent the `in` form left ambiguous, keeps identical semantics on a collection, and
gives the rule nothing to match. This guard holds that shape.

A genuine substring test against a URL is the case the CodeQL rule is right about — if you need
one, parse the URL and compare its host component, rather than reaching back for `in`.

Run: uv run pytest ansible/tests/repo/test_no_host_shaped_membership_literal.py
"""

import ast
import re
import subprocess

from _helpers import REPO

# A literal that looks like a hostname to CodeQL: dot-separated labels ending in a known TLD.
# Kept deliberately close to what the rule matches rather than to what is a valid hostname.
HOST_SHAPED = re.compile(
    r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*\.(io|com|net|org|local|dev|lan)$"
)

SELF = "ansible/tests/repo/test_no_host_shaped_membership_literal.py"

# A substring test against RENDERED TEXT — a manifest, a config file, a command string — is a
# third case, and `in` is the right operator for it: the equality form would iterate the string
# character by character and never be True. CodeQL flags those too, and they are the one shape
# where its complaint is arguable, so each one is named here with the reason rather than left to
# the regex. Add an entry only for a genuine text search, never for a collection.
ALREADY_MITIGATED: dict[str, str] = {}


def _tracked_python_files() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [rel for rel in listed.split("\0") if rel]


def offenders_in(source: str) -> list[int]:
    """Line numbers of `"<host-shaped>" in|not in <anything>` comparisons in `source`."""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Compare):
            continue
        if not isinstance(node.left, ast.Constant) or not isinstance(
            node.left.value, str
        ):
            continue
        if not HOST_SHAPED.match(node.left.value):
            continue
        if any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            hits.append(node.lineno)
    return hits


def test_the_scan_finds_tracked_python_files():
    """Without this, the test below passes vacuously on an empty file list."""
    assert len(_tracked_python_files()) >= 100


def test_no_tracked_file_membership_tests_a_host_shaped_literal():
    found = []
    for rel in _tracked_python_files():
        if rel == SELF or rel in ALREADY_MITIGATED:
            continue
        try:
            tree_hits = offenders_in((REPO / rel).read_text(errors="replace"))
        except SyntaxError:
            continue  # a file this interpreter cannot parse is not this guard's business
        found += [f"{rel}:{line}" for line in tree_hits]
    assert not found, (
        f"{found} test a hostname-shaped literal with `in`, which raises CodeQL "
        f"py/incomplete-url-substring-sanitization. On a list, set or dict write "
        f'`any(x == "<host>" for x in seq)`. On a URL string, parse it and compare the host '
        f"component. On rendered text — a manifest, a config file, a command string — `in` is "
        f"the right operator and the equality form would be wrong: either match a surrounding "
        f"anchor with a regex (see the `apiVersion: traefik\\.io/` search in "
        f"ansible/tests/k8s/test_k8s_manifests_rbac.py) or add the file to ALREADY_MITIGATED "
        f"above with the reason."
    )


def test_the_detector_rejects_the_in_form_and_accepts_the_equality_form():
    """Red-proof pair for `offenders_in` itself."""
    assert offenders_in('assert "backups.longhorn.io" in calls\n') == [1]
    assert offenders_in('assert "www.local.example.com" not in fqdns\n') == [1]
    assert offenders_in('assert any(c == "backups.longhorn.io" for c in calls)\n') == []
    # A literal that is not host-shaped is none of this guard's business.
    assert offenders_in('assert "delete" in argv\n') == []
    # `in` over a literal on the RIGHT is the ordinary lookup direction and never flagged.
    assert offenders_in('assert host in {"a.longhorn.io"}\n') == []
