"""Tests that `probe.py applied` reads no apply record that no role writes (#6987).

#6913 added a per-role record of each STUB render's SHA-256 under `RECORD_DIR`. #6926 deleted
the only task that wrote one, so the reader in `applied.py` has no writer, and the record
`k8s_host_files` wrote stays on daniel-box with a stale hash until daniel-box's package
retires it.
"""

import hashlib
import os

import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diagnostics.probe_lib import applied
from lib import yaml_fast
from lib.exit_codes import PROBE_FAILED
from lib.proc_testing import run
from lib.repo_paths import REPO

RECORD_DIR = "/var/lib/homelab/setup-applied-sha256.d"
APPLIED_PY = "scripts/diagnostics/probe_lib/applied.py"


def _tracked(*pathspecs: str) -> list[str]:
    out = run(
        ["git", "ls-files", "--", *pathspecs],
        cwd=REPO,
        check=True,
    ).stdout
    return [p for p in out.splitlines() if "/tests/" not in p]


def _tasks(node):
    """Every task mapping in a parsed task file, blocks included."""
    if isinstance(node, list):
        for item in node:
            yield from _tasks(item)
    elif isinstance(node, dict):
        yield node
        for key in ("block", "rescue", "always"):
            yield from _tasks(node.get(key))


def _writes_record(task: dict) -> bool:
    for value in task.values():
        if not isinstance(value, dict):
            continue
        target = str(value.get("dest") or value.get("path") or "")
        if target.startswith(RECORD_DIR) and value.get("state") != "absent":
            return True
    return False


def _record_writers() -> list[str]:
    """Tracked non-test files that write under RECORD_DIR; an absent arm is not a writer."""
    writers = []
    for rel in _tracked("ansible", "hosts"):
        path = REPO / rel
        if not path.is_file():
            continue
        text = path.read_text(errors="replace")
        if RECORD_DIR not in text:
            continue
        # Prose and a variable holding the path write nothing; a task or a script does.
        if rel.endswith(".md"):
            continue
        if rel.endswith((".yml", ".yaml")):
            tasks = _tasks(yaml_fast.safe_load(text)) if "/tasks/" in rel else ()
            if any(_writes_record(t) for t in tasks):
                writers.append(rel)
        else:
            writers.append(rel)
    return writers


def test_the_apply_record_has_a_writer_or_no_reader():
    """The Verify-by census: a reader of RECORD_DIR in the probe needs a role that writes it."""
    scripts = _tracked("scripts")
    assert APPLIED_PY in scripts, "the census lost its named member"
    readers = [p for p in scripts if RECORD_DIR in (REPO / p).read_text()]
    writers = _record_writers()
    assert not readers or writers, f"{readers} read {RECORD_DIR}, and no role writes it"


def test_daniel_box_package_retires_the_orphan_apply_record():
    package = yaml_fast.safe_load((REPO / "hosts/daniel-box/package.yml").read_text())
    retired = [entry.get("path") for entry in package["legacy"]]
    orphans = [f"{RECORD_DIR}/k8s_host_files", RECORD_DIR]
    assert all(o in retired for o in orphans), f"no legacy entry removes {orphans}"
    # Reconcile removes only an empty directory, so the file's entry comes first.
    assert retired.index(orphans[0]) < retired.index(orphans[1])


STUB_TASK = """\
- name: Install the token file
  ansible.builtin.template: {src: token.j2, dest: /etc/x/token, mode: "0600", owner: root, group: root}
"""
STUB_CTX = {"x_api_token": "STUB"}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.mark.parametrize(
    "recorded",
    ["token=old\n", "token=current\n"],
    ids=["false-drift", "false-ok"],
)
def test_a_stale_apply_record_gives_no_verdict_on_a_stub_render(
    tmp_path, capsys, recorded
):
    """A record left from an old apply decides neither DRIFT nor ok for the live file."""
    role = tmp_path / "x"
    for sub, name, text in (
        ("tasks", "main.yml", STUB_TASK),
        ("templates", "token.j2", "token={{ x_api_token }}\n"),
    ):
        (role / sub).mkdir(parents=True, exist_ok=True)
        (role / sub / name).write_text(text)
    live = "token=current\n"
    stale = {f"{RECORD_DIR}/x": f"{_sha(recorded)}  /etc/x/token\n"}

    def read(plan: dict, host: str) -> dict:
        row = {"status": "ok", "mode": "0600", "owner": "root", "group": "root"}
        return {
            "host": host,
            "user": "ubuntu",
            "stat": {p: {**row, "sha256": _sha(live)} for p in plan.get("stat", [])},
            "files": {
                p: {"status": "ok", "text": stale[p]}
                if p in stale
                else {"status": "absent"}
                for p in plan.get("files", [])
            },
        }

    ns = SimpleNamespace(role="x", host="daniel-box", dry_run=False)
    code = applied.run_applied(
        ns, read=read, setup=tmp_path, render_ctx=lambda role_dir, host: STUB_CTX
    )
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "/etc/x/token" in ln]
    assert lines and lines[0].split()[0] == "ok-meta", lines
    assert code != PROBE_FAILED
