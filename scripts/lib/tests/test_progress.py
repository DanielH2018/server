from lib.progress import OnChange, count_line


def test_a_repeated_line_is_emitted_once_and_a_changed_line_again():
    out = []
    progress = OnChange(out.append)
    for line in ["waiting on a, b", "waiting on a, b", "waiting on a", "waiting on a"]:
        progress(line)
    assert out == ["waiting on a, b", "waiting on a"]


def test_a_line_that_returns_after_a_change_is_emitted_again():
    out = []
    progress = OnChange(out.append)
    for line in ["held", "clear", "held"]:
        progress(line)
    assert out == ["held", "clear", "held"]


def test_count_line_counts_each_state_in_first_seen_order():
    states = ["running", "done", "running", "needs-input", "running", "done"]
    assert count_line(states) == "3 running, 2 done, 1 needs-input"


def test_count_line_of_nothing_reads_none():
    assert count_line([]) == "none"
