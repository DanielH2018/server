"""Guards on how the weekly AIDE check judges a conffile the host package's lines arm edits.

The lines arm adds a marker-delimited block to a package's conffile, as k3s-server and k3s-agent
add one to /etc/zsh/zshenv, so the file differs from the digest dpkg records. The change is
explained only when removing exactly the declared block, between its markers, leaves the file
dpkg installed. The case table below is the rule.

Run: uv run pytest hosts/tests/test_aide_check_lines.py
"""

import json

import pytest
from _aide_check_run import (
    BLOCK_MARKER,
    CHANGED_CONTENT,
    DECLARED_USERS,
    HOST_MANIFEST,
    LINE_EDITS,
    PACKAGE,
    _report,
    _run_check,
)

CONF = "/etc/tool.conf"
SHIPPED = PACKAGE[CONF][0]
BEGIN, END = (BLOCK_MARKER.replace("{mark}", w) for w in ("BEGIN", "END"))
BLOCK = f"{BEGIN}\nexport K=1\nexport L=2\n{END}\n"
REASON = "differs from its package copy"


def _manifest(edits: list[dict]) -> str:
    return json.dumps({"users": {"users": DECLARED_USERS}, "lines": {"edits": edits}})


ACCEPTED = {
    "the block appended, as the arm adds it": {CONF: SHIPPED + BLOCK},
    "the block before the shipped lines, where an earlier run left it": {
        CONF: BLOCK + SHIPPED
    },
}

REJECTED = {
    "a line outside the block": {CONF: SHIPPED + BLOCK + "export M=3\n"},
    "a changed block body": {CONF: SHIPPED + BLOCK.replace("L=2", "L=3")},
    "a line added inside the block": {
        CONF: SHIPPED + BLOCK.replace("L=2\n", "L=2\nexport M=3\n")
    },
    "the block twice": {CONF: SHIPPED + BLOCK + BLOCK},
    "an END marker with no BEGIN": {CONF: SHIPPED + BLOCK.replace(f"{BEGIN}\n", "")},
    "the markers in a file with no declared block": {
        CONF: SHIPPED + BLOCK,
        HOST_MANIFEST: _manifest([e for e in LINE_EDITS if e["path"] != CONF]),
    },
    "an unreadable lines declaration": {CONF: SHIPPED + BLOCK, HOST_MANIFEST: "{"},
}


@pytest.mark.parametrize("rewrite", ACCEPTED.values(), ids=ACCEPTED.keys())
def test_a_conffile_with_only_its_declared_block_is_clean(tmp_path, rewrite):
    _, pushed, _, _ = _run_check(
        tmp_path, _report(f"{CHANGED_CONTENT}: {CONF}"), rc=4, rewrite=rewrite
    )
    assert pushed[0] == "up", pushed


@pytest.mark.parametrize("rewrite", REJECTED.values(), ids=REJECTED.keys())
def test_a_conffile_with_any_other_difference_is_flagged(tmp_path, rewrite):
    _, pushed, logged, _ = _run_check(
        tmp_path, _report(f"{CHANGED_CONTENT}: {CONF}"), rc=4, rewrite=rewrite
    )
    assert pushed[0] == "down", pushed
    assert f"aide-check: unexplained: {CONF} ({REASON})" in logged, logged
