import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from configarr_status import evaluate, has_error_line, summarize  # noqa: E402


def test_clean_exit_no_errors_is_ok():
    ok, msg = evaluate(0, "Sync started\n3 custom formats, 2 profiles updated\nDone")
    assert ok
    assert "ok" in msg


def test_success_summary_mentioning_errors_word_is_not_a_false_positive():
    # "0 errors" in a success summary must NOT trip the error-line scan (bare-substring bug)
    ok, _ = evaluate(0, "Finished: 0 errors, 2 profiles updated")
    assert ok


def test_nonzero_exit_fails_with_tail():
    ok, msg = evaluate(1, "cloning guides\nUnable to reach http://radarr:7878")
    assert not ok
    assert "exit 1" in msg
    assert "radarr" in msg


def test_error_level_line_on_zero_exit_fails():
    ok, msg = evaluate(0, "Loaded config\nERROR: invalid trash_id foo\nExiting")
    assert not ok
    assert "error" in msg.lower()


def test_bracketed_and_colon_error_levels_trip():
    assert has_error_line("[ERROR] boom")
    assert has_error_line("error: boom")
    assert has_error_line("  FATAL something")


def test_word_errors_at_line_start_is_not_an_error_level():
    assert not has_error_line("errors found: 0")
    assert not has_error_line("Checking for errors...")


def test_empty_output_summarizes_to_placeholder():
    assert summarize("") == "(no output)"
    assert summarize("  \n \n") == "(no output)"
