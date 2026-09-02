"""Every hook script `.claude/settings.json` invokes must be executable in git.

A hook committed 100644 is silently dead: Claude Code runs the `command` string
directly, so the exec bit missing yields "permission denied" on every tool call the
hook matches, and nothing else reports it. `uv-python.sh` shipped that way in #361.

The assertion reads git's index rather than the working tree, because an on-disk
`chmod` that never reaches a commit regresses on the next fresh clone or worktree.
"""

import json
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SETTINGS = REPO / ".claude" / "settings.json"

# Only the command's FIRST token is executed, so only it needs the exec bit. A repo path
# appearing later is an argument to an interpreter (`uv run python .claude/hooks/foo.py`)
# and is read, not executed. Anchoring here keeps the test from demanding a bit that
# shape does not need, and from reaching outside the repo for a `~/.claude/...` hook.
_SCRIPT_RE = re.compile(r"^(?:~/server/|\./)(\.claude/[^\s\"']+\.(?:sh|py))(?:\s|$)")


def _hook_scripts() -> set[str]:
    settings = json.loads(SETTINGS.read_text())
    paths: set[str] = set()
    for matchers in settings.get("hooks", {}).values():
        for matcher in matchers:
            for hook in matcher.get("hooks", []):
                command = hook.get("command", "")
                match = _SCRIPT_RE.match(command.strip())
                if match:
                    paths.add(match.group(1))
    return paths


def _index_modes() -> dict[str, str]:
    out = subprocess.run(
        ["git", "ls-files", "-s", "--", ".claude"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    modes = {}
    for line in out.splitlines():
        fields = line.split("\t", 1)
        if len(fields) == 2:
            modes[fields[1]] = fields[0].split()[0]
    return modes


def test_settings_reference_at_least_one_hook_script():
    # Guards the regex: a parsing change that silently matched nothing would make
    # every assertion below vacuous.
    assert _hook_scripts(), f"no hook scripts parsed out of {SETTINGS}"


def test_hook_scripts_exist_and_are_executable_in_git():
    modes = _index_modes()
    problems = []
    for script in sorted(_hook_scripts()):
        mode = modes.get(script)
        if mode is None:
            problems.append(
                f"{script}: referenced by settings.json but not tracked in git"
            )
        elif mode != "100755":
            problems.append(
                f"{script}: git mode {mode}, expected 100755 (run `chmod +x`)"
            )
    assert not problems, "\n".join(problems)
