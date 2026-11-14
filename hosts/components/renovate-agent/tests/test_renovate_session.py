"""The session unit's script: the worktree gate, the session's token and argv, and its output file.

Run: uv run pytest hosts/components/renovate-agent/tests/test_renovate_session.py
"""

import json

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
    (tmp_path / "exchange" / "requests").mkdir(parents=True)
    (tmp_path / "exchange" / "out").mkdir()
    claude = write_exec(tmp_path / "claude", claude_body)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("/renovate-prs\n")
    cfg = tmp_path / "config.env"
    cfg.write_text(
        f"REPO=o/r\nREPO_DIR={tmp_path}\nPROMPT_FILE={prompt}\n"
        f"EXCHANGE_DIR={tmp_path}/exchange\nCLAUDE_BIN={claude}\n"
    )
    return str(cfg)


def test_the_session_writes_its_output_and_may_write_requests(tmp_path):
    """The output goes to a file, not stdout, and the requests directory is a working directory."""
    seen = tmp_path / "argv"
    body = f'printf "%s\\n" "$@" > {seen}\necho \'{{"type": "result"}}\'\n'
    tools = renovate_agent.AgentTools(run=_Git())

    assert renovate_session.main(tools, _config(tmp_path, body)) == 0

    out = tmp_path / "exchange" / "out" / renovate_session.OUTPUT_FILE
    assert json.loads(out.read_text()) == {"type": "result"}
    argv = seen.read_text().splitlines()
    assert argv[argv.index("--add-dir") + 1] == str(tmp_path / "exchange" / "requests")


def test_a_worktree_holding_work_blocks_the_session(tmp_path):
    ran = tmp_path / "ran"
    tools = renovate_agent.AgentTools(run=_Git(ahead=1))

    renovate_session.main(tools, _config(tmp_path, f"touch {ran}\n"))

    blocked = (
        tmp_path / "exchange" / "out" / renovate_session.BLOCKED_FILE
    ).read_text()
    assert "holds 1 commit(s) not on origin/master" in blocked
    assert not ran.exists()
