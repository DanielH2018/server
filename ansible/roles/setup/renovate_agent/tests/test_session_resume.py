"""A `manual —` hand-off in the digest can be resumed in the session that wrote it (#4603).

`claude -p --output-format json` keeps the transcript and returns its `session_id` in the
result object. The tick has to record that id in `runs.jsonl` and print, beside the hand-off,
the command that resumes it. The harness keys a stored session by its working directory, so
the command must name the run worktree the session ran in.

These drive `main()` end to end with a stand-in `claude` binary, so they hold whatever
signature `render_digest` ends up with.

Run: uv run pytest ansible/roles/setup/renovate_agent/tests/test_session_resume.py
"""

import json
import os
import pathlib
import pwd
import re
import shlex
import subprocess

import agent_logic
import renovate_agent
from gitops_hold import DeployerSnapshot
from lib.proc_testing import write_exec

SESSION_ID = "6f1c2a9e-4b7d-4e2a-9c3f-0d8b5a7e1c42"
HANDOFF = (
    "Left #71 open: manual — bump the immich DB format. The release notes require "
    "a pgvecto.rs migration before the new image starts."
)


class _Host:
    """Answers every process `main()` reaches on a tick that spends a session.

    PR #71 is open before and after, and the run worktree is registered, clean and level with
    origin/master, so the tick reaches `run_session`.
    """

    def __init__(self) -> None:
        self.posts: list[str] = []

    def run(self, argv, cwd=None, timeout=120, stdin_text=None):
        if argv[0] == "gh":
            return 0, json.dumps(
                [
                    {
                        "number": 71,
                        "title": "Update immich (manual — DB migration)",
                        "url": "u/71",
                        "headRefName": "renovate/immich",
                        "updatedAt": "2026-10-12T06:00:00Z",
                        "author": {"login": "app/renovate"},
                    }
                ]
            )
        if "list" in argv and "--porcelain" in argv:
            return (
                0,
                f"worktree {argv[2]}/.claude/worktrees/renovate-auto\nHEAD abc\n\n",
            )
        if "rev-list" in argv:
            return 0, "0\n"
        if "rev-parse" in argv:
            return 0, "0123abcd\n"
        if argv[0] in ("git", "uv"):
            # status, unlock, remove, prune, fetch, add, lock, uv sync: all succeed.
            return 0, ""
        raise AssertionError(
            f"main() reached a process this test does not answer: {argv}"
        )

    def discord_post(self, webhook, text, ua, log=None, spool_dir=None):
        self.posts.append(text)
        return True

    def flush_discord_spool(self, spool_dir, webhook, ua, log=None):
        return True


def _tick(tmp_path: pathlib.Path, summary: str) -> tuple[_Host, pathlib.Path]:
    """Run one tick whose session returns `summary`; return the host and the run worktree."""
    worktree = tmp_path / ".claude" / "worktrees" / "renovate-auto"
    worktree.mkdir(parents=True)
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": summary,
        "session_id": SESSION_ID,
        "total_cost_usd": 3.2,
        "num_turns": 18,
        "permission_denials": [],
        "terminal_reason": "completed",
    }
    claude = write_exec(
        tmp_path / "claude", f"cat <<'EOF'\n{json.dumps(result)}\nEOF\n"
    )
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("/renovate-prs\n")
    cfg = tmp_path / "config.env"
    cfg.write_text(
        f"REPO=o/r\nREPO_DIR={tmp_path}\nPROMPT_FILE={prompt}\n"
        f"STATE_DIR={tmp_path}/state\nCLAUDE_BIN={claude}\n"
    )
    host = _Host()
    tools = renovate_agent.AgentTools(
        run=host.run,
        discord_post=host.discord_post,
        flush_discord_spool=host.flush_discord_spool,
        deployer_state=lambda: DeployerSnapshot(None, None, None, None, None),
    )
    assert renovate_agent.main(tools, str(cfg)) == 0
    return host, worktree


def _resume_lines(digest: str) -> list[str]:
    pattern = re.compile(rf"claude\s+(?:--resume|-r)[\s=]+{re.escape(SESSION_ID)}")
    return [line for line in digest.splitlines() if pattern.search(line)]


def test_the_run_record_carries_the_session_id(tmp_path) -> None:
    _tick(tmp_path, HANDOFF)

    path = tmp_path / "state" / renovate_agent.RUNS_FILE
    (rec,) = [json.loads(line) for line in path.read_text().splitlines()]
    assert rec["result"] == "ran"
    assert rec.get("session_id") == SESSION_ID, rec


def test_the_digest_prints_a_resume_command_beside_a_manual_hand_off(tmp_path) -> None:
    host, _ = _tick(tmp_path, HANDOFF)

    (digest,) = host.posts
    assert "manual — bump the immich DB format" in digest
    assert _resume_lines(digest), digest


def test_the_resume_command_runs_from_the_session_worktree(tmp_path) -> None:
    """The harness finds a stored session only from the directory it ran in."""
    host, worktree = _tick(tmp_path, HANDOFF)

    (digest,) = host.posts
    assert str(worktree) in digest, digest
    # The path belongs to the resume instruction, not merely to some other digest line.
    resume_at = digest.index(_resume_lines(digest)[0])
    assert abs(digest.index(str(worktree)) - resume_at) < 300, digest


def test_the_resume_command_runs_as_the_user_that_owns_the_session(tmp_path) -> None:
    """The harness stores a session under its owner's home, so the operator must switch user.

    The unit runs this process as `renovate_agent_user`, so the effective user here is the
    session's owner.
    """
    owner = pwd.getpwuid(os.geteuid()).pw_name
    host, _ = _tick(tmp_path, HANDOFF)

    (digest,) = host.posts
    as_owner = re.compile(
        rf"(?:sudo\s+(?:-\S+\s+)*-u\s*|runuser\s+-u\s*|su\s+(?:-\s+)?){owner}\b"
    )
    hits = [m.start() for m in as_owner.finditer(digest)]
    assert hits, digest
    # The switch belongs to the resume instruction, not merely to some other digest line.
    resume_at = digest.index(_resume_lines(digest)[0])
    assert any(abs(at - resume_at) < 300 for at in hits), digest


def test_the_resume_command_survives_a_summary_longer_than_discord_allows(
    tmp_path,
) -> None:
    """The summary is cut to fit Discord's 1900 characters; the resume command must not be."""
    host, _ = _tick(tmp_path, HANDOFF + " " + "x" * 5000)

    (digest,) = host.posts
    assert len(digest) < 1900
    assert _resume_lines(digest), digest


def test_a_session_id_is_recorded_only_when_the_result_carries_one() -> None:
    """A missing id must not become an empty field in `runs.jsonl`."""
    moved = agent_logic.delta([], [], {})

    def record(session_id: str) -> dict:
        stdout = json.dumps({"type": "result", "session_id": session_id})
        outcome = agent_logic.parse_run(stdout, 0, False)
        return json.loads(agent_logic.run_record(100, "ran", "", moved, outcome))

    assert record(SESSION_ID)["session_id"] == SESSION_ID
    assert "session_id" not in record("")


def test_the_resumed_claude_gets_the_units_credentials_and_hook_env(tmp_path) -> None:
    """Run the printed command, minus `sudo`, against stub `runuser` and `claude` binaries.

    The credentials are only in the root-owned EnvironmentFile and sudo resets the
    environment, so a command that skips either reaches a claude with no login.
    """
    worktree = tmp_path / "wt"
    worktree.mkdir()
    bindir = tmp_path / "bin"
    seen = tmp_path / "seen"
    write_exec(
        bindir / "runuser",
        f'echo "user=$2" > {seen}\nshift 3\nexec "$@"\n',
    )
    claude = write_exec(
        bindir / "claude",
        f'echo "args=$*" >> {seen}\necho "cwd=$PWD" >> {seen}\nenv >> {seen}\n',
    )
    creds = tmp_path / "creds"
    creds.write_text("GH_TOKEN=gh-sentinel\nCLAUDE_CODE_OAUTH_TOKEN=claude-sentinel\n")
    cmd = agent_logic.resume_command(
        SESSION_ID,
        str(worktree),
        "agent-sentinel",
        str(claude),
        str(creds),
        {"PATH": f"{bindir}:/usr/bin:/bin", "RUN_HOOK_PROJECT_DIR": str(worktree)},
    )
    argv = shlex.split(cmd)
    assert argv[0] == "sudo"
    subprocess.run(
        argv[1:], env={"PATH": f"{bindir}:/usr/bin:/bin"}, check=True, timeout=30
    )

    lines = seen.read_text().splitlines()
    assert "user=agent-sentinel" in lines
    assert f"args=--resume {SESSION_ID}" in lines
    assert f"cwd={worktree}" in lines
    assert "GH_TOKEN=gh-sentinel" in lines
    assert "CLAUDE_CODE_OAUTH_TOKEN=claude-sentinel" in lines
    assert f"RUN_HOOK_PROJECT_DIR={worktree}" in lines


def test_the_resume_carries_every_variable_the_unit_sets() -> None:
    """A variable added to the unit but not to SESSION_ENV_KEYS would be lost on a resume."""
    unit = (
        pathlib.Path(renovate_agent.__file__).parents[1]
        / "templates"
        / "renovate-agent.service.j2"
    ).read_text()
    keys = re.findall(r"^Environment=([A-Z_]+)=", unit, re.MULTILINE)
    assert "RUN_HOOK_PROJECT_DIR" in keys
    assert sorted(keys) == sorted(renovate_agent.SESSION_ENV_KEYS)
    assert re.findall(r"^EnvironmentFile=(\S+)$", unit, re.MULTILINE) == [
        renovate_agent.SESSION_ENV_FILE
    ]
