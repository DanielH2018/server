"""Guards on the AIDE detail the weekly check logs for each entry it pages on.

The journal copy of the report stops at REPORT_MAX_LINES, and a 1M-entry report reaches its
detail section long after that, so the check logs the detail of each unexplained entry apart.

Run: uv run pytest hosts/tests/host_checks/test_aide_check_detail.py
"""

from _aide_check_run import CHANGED_CONTENT, _report, _run_check

DETAIL = """
---------------------------------------------------
Detailed information about changes:
---------------------------------------------------

File: /etc/sudoers.d/extra
 Size      : 4                                | 8
 SHA256    : LIsI2lzmA5jh8Zrw5dzMdE3ydLgmq+WF | w/nIwoOisfLxiW8noBy+PN3Aydk/dS5G
             6rpoxSVDSAY=                     | OQNaD1s29ug=

File: /usr/bin/tool
 Size      : 7                                | 8

"""


def test_an_unexplained_entry_logs_its_detail_past_the_report_cap(tmp_path):
    """The 2026-11-19 page on authorized_keys sat past the cap, so its changed attributes did not show."""
    report = (
        _report(
            f"{CHANGED_CONTENT}: /usr/bin/tool",
            f"{CHANGED_CONTENT}: /etc/sudoers.d/extra",
        )
        + DETAIL
    )
    _, pushed, logged, _ = _run_check(tmp_path, report, rc=4, cap=3)
    assert pushed[0] == "down", pushed
    details = [line for line in logged if line.startswith("aide-check: detail: ")]
    assert details == [
        f"aide-check: detail: /etc/sudoers.d/extra: {CHANGED_CONTENT}",
        "aide-check: detail: /etc/sudoers.d/extra: Size: 4 | 8",
        "aide-check: detail: /etc/sudoers.d/extra: SHA256: "
        "LIsI2lzmA5jh8Zrw5dzMdE3ydLgmq+WF6rpoxSVDSAY= | w/nIwoOisfLxiW8noBy+PN3Aydk/dS5GOQNaD1s29ug=",
    ], details


def test_an_explained_entry_logs_no_detail(tmp_path):
    report = _report(f"{CHANGED_CONTENT}: /usr/bin/tool") + DETAIL
    _, pushed, logged, _ = _run_check(tmp_path, report, rc=4)
    assert pushed[0] == "up", pushed
    assert not [line for line in logged if "detail: " in line], logged
