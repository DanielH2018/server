"""A crashed run must report itself, rather than leave the NotReporting rule to expire.

Issue #1477: the unit's textfile write is its `ExecStartPost`, which systemd runs only when the
process exited 0. A crash wrote nothing, `RenovateAgentNotReporting` fired 28 hours later with no
reason attached, and an operator reading it saw exactly what a host that is simply off looks like.
Two days of that hid a one-line `prepare_worktree` failure. M6 batch B4's Cutover-1a removed the
Kuma push this report used to make, so the reason now reaches the runs file and Discord.

Boundaries come through `renovate_agent.AgentTools` and the config path is a parameter, so
nothing here patches a module attribute.

Run: uv run pytest ansible/roles/setup/renovate_agent/tests/test_crash_report.py
"""

import pathlib

import renovate_agent


class _Recorder:
    """Each curl's argv and stdin, and the Discord bodies, as one injectable pair."""

    def __init__(self) -> None:
        self.curls: list[tuple[list[str], str | None]] = []
        self.posts: list[str] = []
        self.spools: list[str | None] = []

    def tools(self) -> renovate_agent.AgentTools:
        return renovate_agent.AgentTools(
            run=self._run, discord_post=self._post, rmtree=lambda *a, **k: None
        )

    def _run(self, argv, cwd=None, timeout=120, stdin_text=None):
        self.curls.append((argv, stdin_text))
        return 0, ""

    def _post(self, webhook, body, user_agent, log=None, spool_dir=None):
        self.posts.append(body)
        self.spools.append(spool_dir)
        return True


def _config(tmp_path: pathlib.Path) -> str:
    cfg = tmp_path / "config.env"
    lines = ["DISCORD_WEBHOOK=https://discord.example/hook", f"STATE_DIR={tmp_path}"]
    cfg.write_text("\n".join(lines) + "\n")
    return str(cfg)


def test_a_crash_posts_the_exception_text_to_discord_and_the_runs_file(tmp_path):
    rec = _Recorder()

    renovate_agent.report_crash(
        RuntimeError("git worktree add failed: already exists"),
        rec.tools(),
        _config(tmp_path),
    )

    # No curl at all: the Kuma push this report once made went with the tile.
    assert rec.curls == []
    assert rec.posts and "CRASHED" in rec.posts[0] and "already exists" in rec.posts[0]
    # A crash inside an outage is the post the spool exists for (#3905).
    assert rec.spools == [str(tmp_path / "discord-spool")]
    (line,) = (tmp_path / renovate_agent.RUNS_FILE).read_text().splitlines()
    assert '"result": "crashed"' in line and "already exists" in line
