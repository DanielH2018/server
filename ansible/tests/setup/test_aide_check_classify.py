"""Guards on how the weekly AIDE check judges a difference (#4599).

A completed check pushes DOWN on a difference no package explains and keeps the baseline, and
pushes UP and advances the baseline when every difference is a package's, a trigger's or
outside the paged trees. Each judgement is a clean/flagged pair over one report entry, run
against the fake dpkg database `_aide_check_run._fake_host` writes.

Run: uv run pytest ansible/tests/setup/test_aide_check_classify.py
"""

import pytest
from _aide_check_run import (
    ADDED,
    CHANGED_CONTENT,
    CHANGED_MODE,
    CLEAN_REPORT,
    LINK_ADDED,
    REMOVED,
    _report,
    _run_check,
)


# Differences a package, a package trigger or the paged trees' bounds account for.
EXPLAINED = {
    "a package file upgraded in place": f"{CHANGED_CONTENT}: /usr/bin/tool",
    "a /usr/lib file dpkg lists under /lib": (
        f"{CHANGED_CONTENT}: /usr/lib/x86_64-linux-gnu/libtool.so"
    ),
    "a conffile as the package shipped it": f"{CHANGED_CONTENT}: /etc/tool.conf",
    "an alternatives link to a package file": f"{LINK_ADDED}: /etc/alternatives/tool",
    "a file an upgrade dropped": f"{REMOVED}: /usr/bin/old-tool",
    "an initrd the kernel trigger built": f"{ADDED}: /boot/initrd.img-7.0.0-42-generic",
    "an unpackaged /etc file Ansible wrote": f"{ADDED}: /etc/homelab/deployed.env",
    "a file in a home directory": f"{CHANGED_CONTENT}: /home/ubuntu/.zsh_history",
    "a script under /usr/local": f"{ADDED}: /usr/local/bin/helper.sh",
}
# (entry, files rewritten before the run, the reason the journal and the tile give).
FLAGGED = {
    "a package binary that differs from the package": (
        f"{CHANGED_CONTENT}: /usr/bin/tool",
        {"/usr/bin/tool": "trojan\n"},
        "differs from its package copy",
    ),
    "a conffile edited outside the package": (
        f"{CHANGED_CONTENT}: /etc/tool.conf",
        {"/etc/tool.conf": "PermitRootLogin yes\n"},
        "differs from its package copy",
    ),
    "an unpackaged binary in /usr/sbin": (
        f"{ADDED}: /usr/sbin/implant",
        {},
        "no package owns it",
    ),
    "an unpackaged sudoers drop-in": (
        f"{ADDED}: /etc/sudoers.d/extra",
        {},
        "no package owns this login or privilege file",
    ),
    "a changed authorized_keys": (
        f"{CHANGED_CONTENT}: /root/.ssh/authorized_keys",
        {},
        "no package owns this login or privilege file",
    ),
    "a package file that is gone": (
        f"{REMOVED}: /usr/bin/tool",
        {},
        "removed, but a package still owns it",
    ),
    "a package binary whose mode changed": (
        f"{CHANGED_MODE}: /usr/bin/tool",
        {},
        "owner, group or permissions changed",
    ),
}


@pytest.mark.parametrize("entry", EXPLAINED.values(), ids=EXPLAINED.keys())
def test_a_difference_a_package_explains_is_clean(tmp_path, entry):
    code, pushed, _, _ = _run_check(tmp_path, _report(entry), rc=4)
    assert pushed[0] == "up", pushed
    assert code == 0
    assert (tmp_path / "aide-db" / "aide.db").read_text() == "this week\n", (
        "an UP run must advance the baseline, so next week's report covers one week"
    )


@pytest.mark.parametrize(
    ("entry", "rewrite", "reason"), FLAGGED.values(), ids=FLAGGED.keys()
)
def test_a_difference_no_package_explains_is_flagged(tmp_path, entry, rewrite, reason):
    code, pushed, logged, _ = _run_check(
        tmp_path, _report(entry), rc=4, rewrite=rewrite
    )
    path = entry.split(": ", 1)[1]
    assert pushed[0] == "down", pushed
    assert f"{path} ({reason})" in pushed[1], pushed[1]
    assert "aide-check --accept" in pushed[1], pushed[1]
    assert f"aide-check: unexplained: {path} ({reason})" in logged, logged
    assert code == 1
    assert (tmp_path / "aide-db" / "aide.db").read_text() == "last week\n", (
        "a DOWN must keep the baseline, so the change stays in every report until accepted"
    )


# Fails the classifier's join with dpkg's records, the program that reads ARGV[1], and runs
# every other awk call for real.
FAILING_JOIN_AWK = """\
#!/bin/sh
case "$*" in *'ARGV[1]'*) echo 'awk: fatal: out of memory' >&2; exit 2 ;; esac
exec /usr/bin/awk "$@"
"""


def test_a_classifier_that_fails_pushes_down(tmp_path):
    """A failed classifier has judged nothing, so it must not read as all explained."""
    entry = f"{CHANGED_CONTENT}: /usr/bin/tool"
    code, pushed, _, _ = _run_check(
        tmp_path,
        _report(entry),
        rc=4,
        stubs={"awk": FAILING_JOIN_AWK},
    )
    assert pushed[0] == "down", pushed
    assert "classifying its differences failed" in pushed[1], pushed[1]
    assert code == 1
    assert (tmp_path / "aide-db" / "aide.db").read_text() == "last week\n"


def test_accept_moves_the_reviewed_database_into_place(tmp_path):
    code, pushed, logged, ran = _run_check(
        tmp_path, CLEAN_REPORT, rc=0, args=("--accept",), pending=True
    )
    assert code == 0 and not ran and pushed is None
    assert (tmp_path / "aide-db" / "aide.db").read_text() == "reviewed\n"
    assert any("baseline accepted by hand" in line for line in logged), logged


def test_a_missing_baseline_is_never_created(tmp_path):
    """A deleted aide.db must stay an error, not become a fresh, trusted baseline (#6674)."""
    accept, scan = tmp_path / "accept", tmp_path / "scan"
    accept.mkdir()
    scan.mkdir()
    code, _, _, _ = _run_check(
        accept, CLEAN_REPORT, rc=0, args=("--accept",), baseline=False, pending=True
    )
    assert code == 1
    assert not (accept / "aide-db" / "aide.db").exists()
    _, pushed, _, _ = _run_check(scan, CLEAN_REPORT, rc=0, baseline=False)
    assert not (scan / "aide-db" / "aide.db").exists()
    assert "baseline not advanced" in pushed[1], pushed[1]
