"""The triage session script that homelab-claude-triage-session.service runs.

It feeds the controller's prompt to `claude -p` and leaves the JSON result in the exchange.
These tests drive it with a fake `claude` that records what it was given.
"""

import json
import signal
import stat

import claude_triage_session as session
import pytest


def test_a_token_pasted_across_a_wrapped_line_reaches_the_session_whole(
    tmp_path,
) -> None:
    # The credential file as homelab-secrets writes a wrapped paste: a newline and an indent.
    (tmp_path / session.TOKEN_CREDENTIAL).write_text("sk-ant-oat01-abc\n  def\n")
    env = session.session_env({"CREDENTIALS_DIRECTORY": str(tmp_path), "HOME": "/h"})
    assert env[session.TOKEN_ENV] == "sk-ant-oat01-abcdef"
    env = session.session_env(
        {session.TOKEN_ENV: "sk-ant-oat01-abc  def\n", "HOME": "/h"}
    )
    assert env == {session.TOKEN_ENV: "sk-ant-oat01-abcdef", "HOME": "/h"}
    assert session.session_env({"HOME": "/h"}) == {"HOME": "/h"}


def test_the_session_is_diagnose_only_and_capped() -> None:
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`claude-triage@`, a session started by a monitor episode)
    argv = session.build_argv(
        {"CLAUDE_BIN": "claude", "BUDGET_USD": "10", "MAX_TURNS": "60"}
    )
    after = argv[argv.index("--disallowedTools") + 1 :]
    for tool in (
        "Edit",
        "Write",
        "NotebookEdit",
        "Bash(systemctl start homelab-claude-land@*)",
    ):
        assert tool in after
    assert argv[argv.index("--max-budget-usd") + 1] == "10"
    assert argv[argv.index("--max-turns") + 1] == "60"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == session.SCHEMA
    assert "--settings" not in argv
    with_settings = session.build_argv({"SETTINGS_FILE": "/opt/s.json"})
    assert with_settings[with_settings.index("--settings") + 1] == "/opt/s.json"


def test_the_prompt_reaches_claude_on_stdin_and_the_result_lands_in_the_exchange(
    tmp_path, capsys
) -> None:
    """The episode text is on no command line, and nothing the session said is printed."""
    exchange = tmp_path / "exchange"
    (exchange / session.IN_DIR).mkdir(parents=True)
    (exchange / session.OUT_DIR).mkdir()
    (exchange / session.IN_DIR / session.PROMPT_NAME).write_text("episode-sentinel")
    seen = tmp_path / "seen"
    fake = tmp_path / "claude"
    fake.write_text(
        f'#!/bin/sh\necho "$*" > {seen}.argv\ncat > {seen}.stdin\n'
        'echo \'{"result": "session-text-sentinel"}\'\necho stderr-sentinel >&2\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    config = tmp_path / "config.env"
    config.write_text(
        f"EXCHANGE_DIR={exchange}\nCLAUDE_BIN={fake}\nWORKDIR={tmp_path}\n"
    )

    assert session.main(["claude_triage_session.py"], str(config)) == 0

    assert "episode-sentinel" not in (tmp_path / "seen.argv").read_text()
    assert (tmp_path / "seen.stdin").read_text() == "episode-sentinel"
    result = (exchange / session.OUT_DIR / session.RESULT_NAME).read_text()
    assert json.loads(result) == {"result": "session-text-sentinel"}
    printed = capsys.readouterr()
    assert "sentinel" not in printed.out + printed.err


def _transcript_run(tmp_path, claude_body: str) -> tuple:
    """The session script over a fake claude that writes a transcript into a fresh home."""
    exchange = tmp_path / "exchange"
    (exchange / session.IN_DIR).mkdir(parents=True)
    (exchange / session.OUT_DIR).mkdir()
    (exchange / session.IN_DIR / session.PROMPT_NAME).write_text("prompt")
    home = tmp_path / "home"
    transcript = home / ".claude" / "projects" / "-var-lib-clone" / "s1.jsonl"
    transcript.parent.mkdir(parents=True)
    fake = tmp_path / "claude"
    fake.write_text(f"#!/bin/sh\ncat >/dev/null\nT={transcript}\n{claude_body}\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    config = tmp_path / "config.env"
    config.write_text(
        f"EXCHANGE_DIR={exchange}\nCLAUDE_BIN={fake}\nWORKDIR={tmp_path}\n"
        f"SESSION_HOME={home}\n"
    )
    return config, exchange / session.OUT_DIR / session.TRANSCRIPT_NAME


@pytest.fixture
def restore_sigint():
    previous = signal.getsignal(signal.SIGINT)
    yield
    signal.signal(signal.SIGINT, previous)


def test_the_transcript_survives_the_wall_clocks_sigint(
    tmp_path, restore_sigint
) -> None:
    """systemd's stop signals the whole unit, the script included, and the copy still happens."""
    config, copied = _transcript_run(
        tmp_path, 'echo stopped > "$T"\nkill -INT "$PPID"\nsleep 0.2'
    )

    assert session.main(["claude_triage_session.py"], str(config)) == 0

    assert copied.read_text() == "stopped\n"


def test_an_oversized_transcript_is_cut_at_the_cap(tmp_path) -> None:
    transcript = tmp_path / ".claude" / "projects" / "-clone" / "s1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("0123456789abcdef")

    session.export_transcript(str(tmp_path), str(tmp_path), limit=8)

    assert (tmp_path / session.TRANSCRIPT_NAME).read_bytes() == b"01234567"
