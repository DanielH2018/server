"""The hook durations `prek_durations.py` reads from a prek trace log."""

from dev.prek_durations import durations, main, table

LOG = """\
2026-11-19T00:52:17.386926Z TRACE discover{cwd="/repo"}: close time.busy=1.38ms time.idle=1.34µs
2026-11-19T00:52:17.409861Z TRACE matching_filenames{hook="ruff"}: close time.busy=2.01µs time.idle=1.50µs
2026-11-19T00:52:39.336765Z  INFO run{hook_id=shellcheck language=python}: close time.busy=378µs time.idle=471ms
2026-11-19T00:52:39.495927Z  INFO run{hook_id=ruff language=system}: close time.busy=394µs time.idle=1.5s
2026-11-19T00:52:40.495927Z  INFO run{hook_id=ruff language=system}: close time.busy=500ms time.idle=0ns
"""


def test_each_hook_run_span_is_summed_and_other_spans_are_ignored():
    found = durations(LOG)

    assert {hook: round(seconds, 4) for hook, seconds in found.items()} == {
        "shellcheck": 0.4714,
        "ruff": 2.0004,
    }


def test_the_table_counts_every_hook_and_lists_only_the_slowest():
    found = {"a": 1.0, "b": 3.0, "c": 2.0}

    assert table(found, top=2).splitlines() == [
        "3 hooks ran for 6.0s in all; the 2 slowest:",
        "",
        "| hook | seconds |",
        "| --- | ---: |",
        "| `b` | 3.0 |",
        "| `c` | 2.0 |",
    ]


def test_an_unreadable_log_exits_1(tmp_path, capsys):
    status = main([str(tmp_path / "missing.log")])

    assert (status, "cannot read" in capsys.readouterr().err) == (1, True)
