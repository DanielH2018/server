"""`export`: the whole register in `created:` slices, each under gh's list cap.

The fake gh here, unlike `_findings_fakes`, applies the `created:` range and truncates to
`--limit` the way GitHub's search does. A gap or overlap between windows then drops or
repeats a fixture, which an assertion on the argv alone would not see.
"""

import argparse
import json
import re
from datetime import UTC, date, datetime

from dev.findings import main
from dev.findings_lib import export_cli
from dev.findings_lib.boundaries import FindingsTools

_RANGE = re.compile(r"^created:(\d{4}-\d\d-\d\d)\.\.(\d{4}-\d\d-\d\d)$")
_NOW = datetime(2026, 10, 9, 23, 30, tzinfo=UTC)


def _issue(number, created):
    return {"number": number, "createdAt": created, "comments": []}


def _gh(issues, searches):
    def gh_json(*argv, **kwargs):
        search = argv[argv.index("--search") + 1]
        limit = int(argv[argv.index("--limit") + 1])
        searches.append(search)
        if search == "sort:created-asc":
            hits = sorted(issues, key=lambda i: i["createdAt"])
        else:
            match = _RANGE.match(search)
            assert match, search
            start, end = (date.fromisoformat(d) for d in match.groups())
            hits = [
                i
                for i in issues
                if start <= datetime.fromisoformat(i["createdAt"]).date() <= end
            ]
        return hits[:limit]

    return gh_json


def _export(tmp_path, issues, capsys, cap=export_cli.ISSUE_LIST_CAP):
    searches = []
    out = tmp_path / "register.json"
    args = argparse.Namespace(out=str(out), dry_run=False)
    tools = FindingsTools(gh_json=_gh(issues, searches))
    code = export_cli.cmd_export(args, tools, now=lambda: _NOW, cap=cap)
    capsys.readouterr()
    return code, out, searches


def test_every_issue_on_a_window_boundary_is_exported_once(tmp_path, capsys):
    # The oldest issue, the first and last day of a week-long window, and an issue filed
    # late on export day, after midnight UTC by the local clock's reckoning.
    issues = [
        _issue(1, "2026-09-01T00:00:01Z"),
        _issue(2, "2026-09-06T23:59:59Z"),
        _issue(3, "2026-09-07T00:00:00Z"),
        _issue(4, "2026-09-13T12:00:00Z"),
        _issue(5, "2026-10-10T00:10:00Z"),
    ]
    code, out, _ = _export(tmp_path, issues, capsys)
    assert code == 0
    document = json.loads(out.read_text())
    assert [i["number"] for i in document["issues"]] == [1, 2, 3, 4, 5]
    assert document["count"] == 5


def test_a_window_at_the_cap_is_split_until_each_half_comes_back_whole(
    tmp_path, capsys
):
    issues = [_issue(n, f"2026-09-0{n}T10:00:00Z") for n in range(1, 8)]
    code, out, searches = _export(tmp_path, issues, capsys, cap=3)
    assert code == 0
    assert [i["number"] for i in json.loads(out.read_text())["issues"]] == list(
        range(1, 8)
    )
    # The first window, 2026-08-31..09-06, holds six issues, so it was read and split.
    assert "created:2026-08-31..2026-09-06" in searches
    assert "created:2026-08-31..2026-09-03" in searches


def test_a_single_day_at_the_cap_fails_and_writes_no_file(tmp_path, capsys):
    issues = [_issue(n, "2026-09-02T10:00:00Z") for n in range(1, 4)]
    code, out, _ = _export(tmp_path, issues, capsys, cap=2)
    assert code == 1
    assert not out.exists()
    assert list(tmp_path.iterdir()) == []


def test_dry_run_reads_nothing_and_writes_nothing(tmp_path, capsys):
    out = tmp_path / "register.json"

    def gh_json(*argv, **kwargs):
        raise AssertionError(argv)

    assert (
        main(["export", "--out", str(out), "--dry-run"], FindingsTools(gh_json=gh_json))
        == 0
    )
    assert not out.exists()


def test_an_empty_register_writes_an_empty_export(tmp_path, capsys):
    code, out, searches = _export(tmp_path, [], capsys)
    assert code == 0
    assert json.loads(out.read_text())["issues"] == []
    assert searches == ["sort:created-asc"]
