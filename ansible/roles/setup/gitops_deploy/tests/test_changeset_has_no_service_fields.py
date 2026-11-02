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


def test_the_tasks_defer_alert_helpers_are_deleted():
    remediation = importlib.import_module("deployer_lib.deploy_remediation")
    alert_text = importlib.import_module("deployer_lib.deploy_alert_text")
    remediation_name = "deferred_service" + "_alerts"
    alert_name = "tasks_deferred_alert"
    assert not hasattr(remediation, remediation_name)
    assert not hasattr(alert_text, alert_name)


def test_the_alert_slots_have_no_tasks_channel():
    gitops_markers = importlib.import_module("gitops_markers")
    assert "k8s" in gitops_markers.ALERT_SLOTS
    assert "tasks" not in gitops_markers.ALERT_SLOTS


def test_no_handler_is_named_for_the_services_field():
    deploy_handlers = importlib.import_module("deployer_lib.deploy_handlers")
    assert "handle_k8s" in vars(deploy_handlers)
    assert "handle_no_services" not in vars(deploy_handlers)
