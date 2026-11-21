"""Guards on how the weekly AIDE check judges a conffile the host package's lines arm edits.

The lines arm adds a marker-delimited block to a package's conffile, as k3s-server and k3s-agent
add one to /etc/zsh/zshenv, so the file differs from the digest dpkg records. The change is
explained only when removing exactly the declared block, between its markers, leaves the file
dpkg installed. The case table below is the rule.

A conffile the arm only makes line edits to is explained when replaying those edits on the kept
package copy gives the file, the appended lines in any order, and the copy is the one dpkg
records. With no such copy the file is named "original unavailable".

Run: uv run pytest hosts/tests/test_aide_check_lines.py
"""

import json

import pytest
from _aide_fake_host import (
    BLOCK_MARKER,
    DEFS,
    DECLARED_USERS,
    HOST_MANIFEST,
    LINE_EDITS,
    PACKAGE,
)
from _aide_check_run import (
    CHANGED_CONTENT,
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


DEFS_SHIPPED = PACKAGE[DEFS][0]
DEFS_EDITED = (
    DEFS_SHIPPED.replace("022", "027") + "PASS_MIN_DAYS\t1\nPASS_WARN_AGE\t14\n"
)

LINES_ACCEPTED = {
    "the edits replayed in manifest order": DEFS_EDITED,
    "the appended lines in the order the arm met them": DEFS_SHIPPED.replace(
        "022", "027"
    )
    + "PASS_WARN_AGE\t14\nPASS_MIN_DAYS\t1\n",
}

LINES_REJECTED = {
    "an undeclared line appended": DEFS_EDITED + "PASS_MAX_DAYS\t30\n",
    "a declared line with another value": DEFS_EDITED.replace(
        "WARN_AGE\t14", "WARN_AGE\t1"
    ),
    "a shipped line removed": DEFS_EDITED.replace("# limits\n", ""),
    "a declared line missing": DEFS_EDITED.replace("PASS_MIN_DAYS\t1\n", ""),
    "an appended line moved above the shipped ones": "PASS_MIN_DAYS\t1\n"
    + DEFS_EDITED.replace("PASS_MIN_DAYS\t1\n", ""),
}


@pytest.mark.parametrize("now", LINES_ACCEPTED.values(), ids=LINES_ACCEPTED.keys())
def test_a_conffile_the_declared_line_edits_give_is_clean(tmp_path, now):
    _, pushed, _, _ = _run_check(
        tmp_path,
        _report(f"{CHANGED_CONTENT}: {DEFS}"),
        rc=4,
        rewrite={DEFS: now},
    )
    assert pushed[0] == "up", pushed


@pytest.mark.parametrize("now", LINES_REJECTED.values(), ids=LINES_REJECTED.keys())
def test_a_conffile_with_an_undeclared_line_difference_is_flagged(tmp_path, now):
    _, pushed, logged, _ = _run_check(
        tmp_path,
        _report(f"{CHANGED_CONTENT}: {DEFS}"),
        rc=4,
        rewrite={DEFS: now},
    )
    assert pushed[0] == "down", pushed
    assert f"aide-check: unexplained: {DEFS} ({REASON})" in logged, logged


UNAVAILABLE = {
    "no copy kept, and the archive unreachable": {},
    "a kept copy that is not the package's": {
        DEFS: DEFS_SHIPPED.replace("# limits", "#")
    },
}


@pytest.mark.parametrize("originals", UNAVAILABLE.values(), ids=UNAVAILABLE.keys())
def test_a_line_edited_conffile_with_no_usable_original_is_flagged(tmp_path, originals):
    _, pushed, logged, _ = _run_check(
        tmp_path,
        _report(f"{CHANGED_CONTENT}: {DEFS}"),
        rc=4,
        rewrite={DEFS: DEFS_EDITED},
        originals=originals,
    )
    assert pushed[0] == "down", pushed
    assert (
        f"aide-check: unexplained: {DEFS} ({REASON}; original unavailable)" in logged
    ), logged


def test_a_judge_that_fails_is_named_apart_from_a_content_difference(tmp_path):
    unparsable = [{**e, "regexp": "("} if e["path"] == DEFS else e for e in LINE_EDITS]
    _, pushed, logged, _ = _run_check(
        tmp_path,
        _report(f"{CHANGED_CONTENT}: {DEFS}"),
        rc=4,
        rewrite={DEFS: DEFS_EDITED, HOST_MANIFEST: _manifest(unparsable)},
    )
    assert pushed[0] == "down", pushed
    assert (
        f"aide-check: unexplained: {DEFS} ({REASON}; aide-originals judge failed, so the "
        "edits were not replayed; journalctl -t aide-check has its error)" in logged
    ), logged


def test_a_run_tries_to_keep_each_missing_copy_before_it_scans(tmp_path):
    _, _, logged, _ = _run_check(
        tmp_path, _report(f"{CHANGED_CONTENT}: {DEFS}"), rc=4, originals={}
    )
    assert (
        "aide-check: package copies of edited conffiles: 0 fetched, 1 not kept"
        in logged
    ), logged
    assert (
        f"aide-check: not kept: {DEFS} (apt-get exited 100: E: no network in the suite)"
        in logged
    ), logged
