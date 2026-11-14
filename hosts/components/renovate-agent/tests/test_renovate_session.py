"""The session unit's script: the worktree gate, the session's token and argv, and its output file.

Run: uv run pytest hosts/components/renovate-agent/tests/test_renovate_session.py
"""

import json

import pytest
import renovate_agent
import renovate_session
from lib.proc_testing import write_exec


class _Git:
    """A clean, registered run worktree level with origin/master, or one holding a commit."""

    def __init__(self, ahead: int = 0) -> None:
        self.ahead = ahead

    def __call__(self, argv, cwd=None, timeout=120, stdin_text=None):
        if "list" in argv and "--porcelain" in argv:
            return (
                0,
                f"worktree {argv[2]}/.claude/worktrees/renovate-auto\nHEAD abc\n\n",
            )
        if "rev-list" in argv:
            return 0, f"{self.ahead}\n"
        if "rev-parse" in argv:
            return 0, "0123abcd\n"
        if "merge-tree" in argv:
            return 0, "89abcdef\n"
        return 0, ""


def _config(tmp_path, claude_body: str) -> str:
    (tmp_path / ".claude" / "worktrees" / "renovate-auto").mkdir(parents=True)
    (tmp_path / "state" / "requests").mkdir(parents=True)
    claude = write_exec(tmp_path / "claude", claude_body)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("/renovate-prs\n")
    cfg = tmp_path / "config.env"
    cfg.write_text(
        f"REPO=o/r\nREPO_DIR={tmp_path}\nPROMPT_FILE={prompt}\n"
        f"STATE_DIR={tmp_path}/state\nCLAUDE_BIN={claude}\n"
    )
    return str(cfg)


@pytest.fixture(autouse=True)
def _session_token(monkeypatch):
    monkeypatch.setenv("RENOVATE_SESSION_GH_TOKEN", "gh-read-sentinel")


def test_the_session_writes_its_output_and_may_write_requests(tmp_path):
    """The output goes to a file, not stdout, and the requests directory is a working directory."""
    seen = tmp_path / "argv"
    body = f'printf "%s\\n" "$@" > {seen}\necho \'{{"type": "result"}}\'\n'
    tools = renovate_agent.AgentTools(run=_Git())

    assert renovate_session.main(tools, _config(tmp_path, body)) == 0

    out = tmp_path / "state" / renovate_session.OUTPUT_FILE
    assert json.loads(out.read_text()) == {"type": "result"}
    argv = seen.read_text().splitlines()
    assert argv[argv.index("--add-dir") + 1] == str(tmp_path / "state" / "requests")


def test_a_worktree_holding_work_blocks_the_session(tmp_path):
    ran = tmp_path / "ran"
    tools = renovate_agent.AgentTools(run=_Git(ahead=1))

    renovate_session.main(tools, _config(tmp_path, f"touch {ran}\n"))

    blocked = (tmp_path / "state" / renovate_session.BLOCKED_FILE).read_text()
    assert "holds 1 commit(s) not on origin/master" in blocked
    assert not ran.exists()


def test_the_session_gets_the_read_only_token_as_gh_token() -> None:
    env = renovate_session.session_env(
        {"GH_TOKEN": "write", "RENOVATE_SESSION_GH_TOKEN": "read", "PATH": "/bin"}
    )
    assert env == {"GH_TOKEN": "read", "PATH": "/bin"}


def test_a_session_without_its_read_token_never_starts() -> None:
    with pytest.raises(RuntimeError, match="RENOVATE_SESSION_GH_TOKEN is unset"):
        renovate_session.session_env({"GH_TOKEN": "write"})
