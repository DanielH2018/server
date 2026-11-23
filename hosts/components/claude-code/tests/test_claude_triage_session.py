"""The triage session script that homelab-claude-triage-session.service runs.

It feeds the controller's prompt to `claude -p` and leaves the JSON result in the exchange.
These tests drive it with a fake `claude` that records what it was given.
"""

import json
import signal
import stat

import claude_triage_session as session
import host_lib
import pytest


def _published(tmp_path) -> tuple[str, str]:
    """A published config as homelab-claude-config-render leaves it; (SETTINGS_FILE, its target)."""
    root = tmp_path / "published"
    version = root / "versions" / "v1"
    version.mkdir(parents=True)
    (version / "triage.json").write_text("{}")
    (root / "lock").touch()
    (root / "current").symlink_to("versions/v1")
    return str(root / "current" / "triage.json"), str(version / "triage.json")


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
        {"CLAUDE_BIN": "claude", "BUDGET_USD": "10", "MAX_TURNS": "60"}, "/s.json"
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
    assert argv[argv.index("--settings") + 1] == "/s.json"


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
    settings, target = _published(tmp_path)
    config = tmp_path / "config.env"
    config.write_text(
        f"EXCHANGE_DIR={exchange}\nCLAUDE_BIN={fake}\nWORKDIR={tmp_path}\n"
        f"SETTINGS_FILE={settings}\n"
    )

    assert session.main(["claude_triage_session.py"], str(config)) == 0

    assert f"--settings {target} " in (tmp_path / "seen.argv").read_text()
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
        f"SESSION_HOME={home}\nSETTINGS_FILE={_published(tmp_path)[0]}\n"
    )
    return config, exchange / session.OUT_DIR / host_lib.TRANSCRIPT_NAME


def test_no_session_starts_without_the_published_settings(tmp_path) -> None:
    """A missing publish leaves the session with no deny floor, so claude never starts."""
    started = tmp_path / "started"
    config, _ = _transcript_run(tmp_path, f"touch {started}")
    (tmp_path / "published" / "current").unlink()

    with pytest.raises(RuntimeError, match="no published Claude Code settings"):
        session.main(["claude_triage_session.py"], str(config))

    assert not started.exists()


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


def test_the_result_may_name_one_restart_and_need_not():
    argv = session.build_argv({}, "/s.json")
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert schema["properties"]["restart"] == {"type": "string"}
    assert "restart" not in schema["required"]
