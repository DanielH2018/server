"""Every headless fan-out session answers its permission prompts through claude-guard.

`fanout_lib.prompt_tool` says why. A session whose prompt-tool server cannot start exits
before its first turn, so the launch refuses when the shim is missing.

Run: uv run pytest scripts/dev/tests/test_fanout_prompt_tool.py
"""

import json
import shlex

from _review_fakes import _pipeline
from fanout_lib.launch import systemd_run_command
from fanout_lib.prompt_tool import PROMPT_TOOL, shim_guarded
from fanout_lib.target import Target
from lib.proc_testing import run, write_exec


def _mcp_config(argv: list[str]) -> dict:
    return json.loads(argv[argv.index("--mcp-config") + 1])


def test_every_review_pipeline_phase_names_the_prompt_tool(tmp_path):
    pipeline, _ = _pipeline(tmp_path, [])
    pipeline.session = "sid-1"
    phases = (
        pipeline._implementer(),
        pipeline._red_author(),
        pipeline._resume(),
        pipeline._reviewer(),
    )
    for argv in phases:
        assert argv[argv.index("--permission-prompt-tool") + 1] == PROMPT_TOOL
        server = _mcp_config(argv)["mcpServers"]["claude-guard"]
        assert server["args"] == ["prompt-server"]
        assert server["command"].endswith("/.local/bin/claude-guard")
        assert "--strict-mcp-config" not in argv


def test_a_plain_batch_names_the_shim_under_its_own_users_home():
    dotfiles = Target("DanielH2018/dotfiles", "/c", "origin/main")
    for target in (None, dotfiles):
        args = ("b",) if target is None else ("b", target)
        cmd = systemd_run_command(*args, home="/var/lib/claude")
        argv = shlex.split(cmd.split("; then ", 1)[1].split(" ; else ", 1)[0])
        assert argv[argv.index("--permission-prompt-tool") + 1] == PROMPT_TOOL
        server = _mcp_config(argv)["mcpServers"]["claude-guard"]
        assert server["command"] == "/var/lib/claude/.local/bin/claude-guard"


def test_the_launch_runs_when_the_shim_is_executable(tmp_path):
    write_exec(tmp_path / ".local" / "bin" / "claude-guard", "exit 0\n")
    r = run(shim_guarded("echo started", str(tmp_path)), shell=True)
    assert (r.returncode, r.stdout.strip()) == (0, "started")


def test_the_launch_is_refused_naming_the_shim_when_it_is_missing(tmp_path):
    r = run(shim_guarded("echo started", str(tmp_path)), shell=True)
    assert r.returncode != 0
    assert "started" not in r.stdout
    assert f"no claude-guard shim at {tmp_path}/.local/bin/claude-guard" in r.stderr
