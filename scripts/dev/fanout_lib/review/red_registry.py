"""What a fix may change in a patch registry the red commits touched (#4875).

`forbidden_registry_change` lets a fix change a file in
`REGISTRIES` that the red commits touched only in the lines whose first field names a file the
red commits added. The monkeypatch ratchet needs a `<path> <count>` line for every test module
that patches, so a fix that appends a patching test to a red file must raise that file's count.
In #5397 the red commits added `scripts/dev/tests/test_cross_plane_replay_renderer_only.py 8`
and the fix raised it to 9, which the gate refused as an edit to a red data file. Every other
line, comments and blank lines included, must stay, so a fix cannot add, raise or delete
another module's entry. Lines compare as a multiset, because the ratchet in CI checks the order.
Dropping a red file's own line passes here too; the ratchet refuses it while the file patches.

Split from `red_lock`, which says what a fix may change in a red test file and a conftest.
"""

from collections import Counter
from collections.abc import Iterable

# Ratchet files of `<path> <count>` lines, where a red test module may need registering.
REGISTRIES = frozenset({"ansible/tests/repo/monkeypatch_allowlist.txt"})


def forbidden_registry_change(red_src: str, head_src: str, added: Iterable[str]) -> str:
    """Why `head_src` changes a line naming no file the red commits added, or "".

    Args:
        red_src: the registry as the red commits left it.
        head_src: the same file at the implementer's HEAD.
        added: the files the red commits created.
    """
    red, head, ours = (
        Counter(red_src.splitlines()),
        Counter(head_src.splitlines()),
        set(added),
    )
    for verb, lines in (("drops", red - head), ("adds", head - red)):
        for line in sorted(lines):
            if not set(line.split()[:1]) & ours:
                return (
                    f"{verb} `{line}`, a line that names no file the red commits added"
                )
    return ""
