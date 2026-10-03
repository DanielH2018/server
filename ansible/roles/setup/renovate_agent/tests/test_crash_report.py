"""A crashed run must report itself, rather than leave the deadman to expire.

Issue #1477: the alive beat is the unit's `ExecStartPost`, which systemd runs only when the
process exited 0. A crash pushed nothing, the `Renovate Agent — Alive` monitor went down 28
hours later by deadman expiry with no reason attached, and an operator reading it saw exactly
what a host that is simply off looks like. Two days of that hid a one-line `prepare_worktree`
failure.

Boundaries come through `renovate_agent.AgentTools` and the config path is a parameter, so
nothing here patches a module attribute.

Run: uv run pytest ansible/roles/setup/renovate_agent/tests/test_crash_report.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "files"))
import renovate_agent

PUSH_URL = "https://kuma.example/api/push/abc123"


class _Recorder:
    """Each curl's argv and stdin, and the Discord bodies, as one injectable pair."""

    def __init__(self) -> None:
        self.curls: list[tuple[list[str], str | None]] = []
        self.posts: list[str] = []

    def tools(self) -> renovate_agent.AgentTools:
        return renovate_agent.AgentTools(
            run=self._run, discord_post=self._post, rmtree=lambda *a, **k: None
        )

    def _run(self, argv, cwd=None, timeout=120, stdin_text=None):
        self.curls.append((argv, stdin_text))
        return 0, ""

    def _post(self, webhook, body, user_agent, log=None):
        self.posts.append(body)
        return True


def _config(tmp_path: pathlib.Path, *, push_url: str) -> str:
    cfg = tmp_path / "config.env"
    lines = ["DISCORD_WEBHOOK=https://discord.example/hook", f"STATE_DIR={tmp_path}"]
    if push_url:
        lines.append(f"KUMA_PUSH_URL={push_url}")
    cfg.write_text("\n".join(lines) + "\n")
    return str(cfg)


def test_a_crash_pushes_a_down_carrying_the_exception_text(tmp_path):
    rec = _Recorder()

    renovate_agent.report_crash(
        RuntimeError("git worktree add failed: already exists"),
        rec.tools(),
        _config(tmp_path, push_url=PUSH_URL),
    )

    ((argv, stdin_text),) = rec.curls
    assert argv[0] == "curl"
    assert any("git worktree add failed" in arg for arg in argv)
    # The URL carries the push token, and a running curl's argv is readable by any local user.
    assert not any(PUSH_URL in arg for arg in argv)
    assert stdin_text == f'url = "{PUSH_URL}?status=down"\n'
    assert argv[argv.index("-K") + 1] == "-"
    assert rec.posts and "CRASHED" in rec.posts[0] and "already exists" in rec.posts[0]
    (line,) = (tmp_path / renovate_agent.RUNS_FILE).read_text().splitlines()
    assert '"result": "crashed"' in line and "already exists" in line


def test_a_config_with_no_push_url_still_posts_to_discord(tmp_path):
    """The rejecting half: no push URL means no curl at all, never a curl to an empty URL.

    A `curl --get ?status=down` against "" is a request to the local filesystem, and its `-f`
    failure reads in the journal exactly like a Kuma outage.
    """
    rec = _Recorder()

    renovate_agent.report_crash(
        RuntimeError("boom"), rec.tools(), _config(tmp_path, push_url="")
    )

    assert rec.curls == []
    assert rec.posts and "boom" in rec.posts[0]
