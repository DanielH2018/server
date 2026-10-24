"""`scripts/validate/notification_renders.py`, with amtool faked.

The pytest shards install no amtool, so the passing runner answers each render with its
expected text. CI's tool phase (`alert_rules.py`) runs the real amtool against the same files.
"""

from validate import notification_renders


def _tools_pass(argv: list[str]) -> tuple[int, str]:
    """A runner under which each notification render prints its expected text."""
    for check, expected in notification_renders.render_checks():
        if check == argv:
            return 0, expected.read_text()
    return 0, ""


def test_notification_renders_that_match_their_expected_text_are_clean():
    # Non-vacuity: the floor names the three shapes, and each has every define's render.
    assert len(notification_renders.render_checks()) == 3 * len(
        notification_renders.RENDER_DEFINES
    )
    assert notification_renders.render_problems(run=_tools_pass) == []


def test_a_notification_render_that_differs_from_its_expected_text_is_flagged():
    def runner(argv: list[str]) -> tuple[int, str]:
        code, output = _tools_pass(argv)
        if argv[-1] == '--template.text={{ template "homelab.message" . }}':
            return code, output.replace("**Resolved (1)**", "Resolved")
        return code, output

    problems = notification_renders.render_problems(run=runner)
    assert len(problems) == 1, problems
    assert "renders mixed.homelab.message.txt differently" in problems[0]


def test_a_render_case_directory_missing_a_floor_group_is_flagged(tmp_path):
    (tmp_path / "mixed.json").write_text("{}")
    problems = notification_renders.render_problems(run=_tools_pass, cases=tmp_path)
    assert any("'resolved', 'shared-description'" in p for p in problems), problems
