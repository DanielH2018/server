"""The session unit's script: the session's argv, its working directory and its output file.

Run: uv run pytest hosts/components/renovate-agent/tests/test_renovate_session.py
"""

import json

import renovate_session
from lib.proc_testing import write_exec


def _config(tmp_path, claude_body: str) -> str:
    (tmp_path / "exchange" / "requests").mkdir(parents=True)
    (tmp_path / "exchange" / "out").mkdir()
    claude = write_exec(tmp_path / "claude", claude_body)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("/renovate-prs\n")
    cfg = tmp_path / "config.env"
    cfg.write_text(
        f"REPO=o/r\nREPO_DIR={tmp_path}\nPROMPT_FILE={prompt}\n"
        f"EXCHANGE_DIR={tmp_path}/exchange\nCLAUDE_BIN={claude}\n"
        "SETTINGS_FILE=/usr/lib/homelab/renovate-agent/claude-settings.json\n"
    )
    return str(cfg)


def test_the_session_writes_its_output_and_may_write_requests(tmp_path):
    """The output goes to a file, not stdout, the requests directory is a working directory,
    and the session runs in the clone with the packaged settings."""
    seen = tmp_path / "argv"
    body = (
        f'printf "%s\\n" "$@" > {seen}\npwd > {seen}.cwd\n'
        'echo \'{"type": "result"}\'\n'
    )

    assert renovate_session.main(_config(tmp_path, body)) == 0

    out = tmp_path / "exchange" / "out" / renovate_session.OUTPUT_FILE
    assert json.loads(out.read_text()) == {"type": "result"}
    argv = seen.read_text().splitlines()
    assert argv[argv.index("--add-dir") + 1] == str(tmp_path / "exchange" / "requests")
    assert argv[argv.index("--settings") + 1] == (
        "/usr/lib/homelab/renovate-agent/claude-settings.json"
    )
    assert (tmp_path / "argv.cwd").read_text().strip() == str(tmp_path)
