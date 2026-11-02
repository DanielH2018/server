"""No deploy tool reads `ChangeSet.services` or `ChangeSet.tasks`, which no path sets (#6980)."""

import re
import subprocess
from pathlib import Path

from lib.repo_paths import REPO

TOOLS_DIR = "scripts/deploy_tools"
# Split so this file does not match its own census.
READER_PATTERN = re.compile(r"cs\.(?:services|tasks)" + "|deferred_service" + "_alerts")


def test_no_deploy_tool_reads_the_deleted_fields():
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", TOOLS_DIR],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    files = sorted(p for p in out.split("\0") if p.endswith(".py"))
    assert f"{TOOLS_DIR}/narrow_lib/broad.py" in files
    assert f"{TOOLS_DIR}/deploy_lib/tags.py" in files
    own = Path(__file__).resolve().relative_to(REPO).as_posix()
    hits = [
        f"{rel}:{n}: {line.strip()}"
        for rel in files
        if rel != own
        for n, line in enumerate((REPO / rel).read_text().splitlines(), 1)
        if READER_PATTERN.search(line)
    ]
    assert hits == []
