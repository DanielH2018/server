"""The effort every fan-out session runs at: medium or lower, pinned on its command line.

The operator ruled on 2026-10-18 that no fan-out session runs above medium. Each phase passes
`--effort` itself, because an inherited `effortLevel` would rise with the next settings edit.
The reviewer's own effort test sits in `test_fanout_review.py`, beside its record.

Run: uv run pytest scripts/dev/tests/test_fanout_effort.py
"""

from _review_fakes import _pipeline
from fanout_lib.launch import systemd_run_command

ALLOWED = {"low", "medium"}


def test_every_review_pipeline_phase_pins_medium_effort_or_lower(tmp_path):
    pipeline, _ = _pipeline(tmp_path, [])
    pipeline.session = "sid-1"
    for argv in (pipeline._implementer(), pipeline._red_author(), pipeline._resume()):
        assert argv[argv.index("--effort") + 1] in ALLOWED


def test_an_unreviewed_batch_pins_medium_effort():
    argv = systemd_run_command("b").split()
    assert argv[argv.index("--effort") + 1] in ALLOWED
