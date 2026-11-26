"""Guards on how the weekly AIDE check judges a conffile the host package's lines arm edits.

The lines arm adds a marker-delimited block to a package's conffile, as k3s-server and k3s-agent
add one to /etc/zsh/zshenv, so the file differs from the digest dpkg records. The change is
explained only when removing exactly the declared blocks, each between its markers, leaves the
file dpkg installed. The case table below is the rule.

A conffile the arm only makes line edits to is explained when replaying those edits on a kept
package copy of any version gives the file, the appended lines in any order. With no copy kept
for the package that lists it, the file is named "original unavailable".

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
    TOOL_COPY,
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


# A second component's block on the same conffile, as claude-code adds one to /etc/zsh/zshenv.
SECOND_MARKER = "# {mark} MANAGED — tool oom score"
SECOND_BEGIN, SECOND_END = (
    SECOND_MARKER.replace("{mark}", w) for w in ("BEGIN", "END")
)
SECOND = f"{SECOND_BEGIN}\nexport S=1\n{SECOND_END}\n"
TWO_BLOCKS = {
    HOST_MANIFEST: _manifest(
        [*LINE_EDITS, {"path": CONF, "marker": SECOND_MARKER, "block": "export S=1\n"}]
    )
}

ACCEPTED = {
    "the block appended, as the arm adds it": {CONF: SHIPPED + BLOCK},
    "the block before the shipped lines, where an earlier run left it": {
        CONF: BLOCK + SHIPPED
    },
    "two declared blocks": {CONF: SHIPPED + BLOCK + SECOND, **TWO_BLOCKS},
    "two declared blocks around the shipped lines": {
        CONF: SECOND + SHIPPED + BLOCK,
        **TWO_BLOCKS,
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
    "one of two declared blocks missing": {CONF: SHIPPED + BLOCK, **TWO_BLOCKS},
    "a changed body in the first of two declared blocks": {
        CONF: SHIPPED + BLOCK.replace("L=2", "L=3") + SECOND,
        **TWO_BLOCKS,
    },
    "a changed body in the second of two declared blocks": {
        CONF: SHIPPED + BLOCK + SECOND.replace("S=1", "S=2"),
        **TWO_BLOCKS,
    },
    "two declared blocks with swapped bodies": {
        CONF: SHIPPED
        + BLOCK.replace("export K=1\nexport L=2", "export S=1")
        + SECOND.replace("export S=1", "export K=1\nexport L=2"),
        **TWO_BLOCKS,
    },
    "one declared block nested in the other": {
        CONF: SHIPPED + BLOCK.replace(f"{END}\n", f"{SECOND}{END}\n"),
        **TWO_BLOCKS,
    },
    "a line between two declared blocks": {
        CONF: SHIPPED + BLOCK + "export M=3\n" + SECOND,
        **TWO_BLOCKS,
    },
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


OLDER_SHIPPED = DEFS_SHIPPED.replace("# limits", "# login limits")
OLDER_EDITED = DEFS_EDITED.replace("# limits", "# login limits")
OLDER_COPY = TOOL_COPY.replace("/1.0/", "/0.9/")


def test_a_conffile_the_edits_give_from_an_older_versions_copy_is_clean(tmp_path):
    _, pushed, _, _ = _run_check(
        tmp_path,
        _report(f"{CHANGED_CONTENT}: {DEFS}"),
        rc=4,
        rewrite={DEFS: OLDER_EDITED},
        originals={TOOL_COPY: DEFS_SHIPPED, OLDER_COPY: OLDER_SHIPPED},
    )
    assert pushed[0] == "up", pushed


UNAVAILABLE = {
    "no copy kept, and the archive unreachable": {},
    "a kept copy that is not the package's": {
        TOOL_COPY: DEFS_SHIPPED.replace("# limits", "#")
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
