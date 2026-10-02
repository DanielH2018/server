from _setup_render import render_setup_text


def test_probe():
    for t in (
        "fleet-slice-caps.conf.j2",
        "claude-rc.service.j2",
        "login-slice-caps.conf.j2",
        "pytest-fanout-cap.conf.j2",
        "claude-memory-sync.service.j2",
    ):
        print()
        print("=== PROBE", t)
        print(render_setup_text("claude_code", t))
