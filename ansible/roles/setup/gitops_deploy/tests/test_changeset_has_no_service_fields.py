"""`ChangeSet.services` and `ChangeSet.tasks` are gone, with every reader of them (#6980).

H5.13 deleted the containers arm that was the only writer of both fields, so each reader below
guards a state no changed path can produce.
"""

import dataclasses
import importlib
import re
import subprocess
from pathlib import Path

from lib.repo_paths import REPO

ROLE_DIR = "ansible/roles/setup/gitops_deploy"
# Split so this file does not match its own census.
READER_PATTERN = re.compile(r"cs\.(?:services|tasks)" + "|deferred_service" + "_alerts")


def _tracked_files(prefix: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", prefix],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def _reader_hits(prefix: str) -> list[str]:
    own = Path(__file__).resolve().relative_to(REPO).as_posix()
    files = _tracked_files(prefix)
    assert f"{prefix}/files/deployer_lib/deploy_changes.py" in files
    hits = []
    for rel in files:
        if rel == own or not rel.endswith((".py", ".md", ".j2", ".yml", ".sh")):
            continue
        for n, line in enumerate(
            (REPO / rel).read_text(errors="replace").splitlines(), 1
        ):
            if READER_PATTERN.search(line):
                hits.append(f"{rel}:{n}: {line.strip()}")
    return hits


def test_no_file_in_the_role_reads_the_deleted_fields():
    assert _reader_hits(ROLE_DIR) == []


def test_changeset_declares_neither_services_nor_tasks():
    deploy_changes = importlib.import_module("deployer_lib.deploy_changes")
    names = {f.name for f in dataclasses.fields(deploy_changes.ChangeSet)}
    assert "k8s" in names
    assert names.isdisjoint({"services", "tasks"})
