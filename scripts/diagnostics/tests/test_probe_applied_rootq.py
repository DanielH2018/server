"""Tests for `probe.py applied` reading root-only files through rootq (#6357).

Each test runs `run_applied` on its real host read. The role's files sit under a directory
this user cannot traverse, so `lstat` fails with EACCES, the way the live run reads
`/etc/polkit-1/rules.d`. The run must not reach any real rootq or ssh. Three fakes go first on
`PATH`: `rootq`, `ssh` and `sudo`. Fake `ssh` drops its options and host and runs the command
here. Fake `rootq` answers from a table the test writes and logs every argv it gets. It
answers as rootq does. `stat` prints the metadata line, and `matches PATH FILE` compares the
file's hash with the table's content. A `*.env` path is refused as on the deny list.

The contract the fix meets: `applied` runs rootq by its `PATH` name, `rootq`, over the host
read's transport or its own.
"""

import json
import os
import socket
import stat
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from diagnostics.probe_lib import applied
from lib.exit_codes import PROBE_FAILED, PROBE_OK
from lib.exit_codes import PROBE_UNREADABLE
from lib.proc_testing import path_with, write_exec

SCRIPT = "#!/bin/sh\nexec x --port {{ port }}\n"
RENDERED = "#!/bin/sh\nexec x --port 8080\n"
CTX = {"port": 8080}
# The operator's ruling on #6357: tier 1, and of tier 1 only these two verbs.
ALLOWED_VERBS = {"stat", "matches"}

FAKE_ROOTQ = r"""#!@PYTHON@
import hashlib, json, re, sys
args = sys.argv[1:]
with open("@LOG@", "a") as log:
    log.write(json.dumps(args) + "\n")
table = json.load(open("@TABLE@"))
verb, rest = (args[0], args[1:]) if args else ("", [])
if verb not in ("stat", "sha256", "matches"):
    # A printing verb: answer it, so a fix that prints is caught by the log, not by an error.
    print(table.get(rest[0], {}).get("content", "") if rest else "")
    sys.exit(0)
path = rest[0] if rest else ""
if path.endswith(".env"):
    print(f"refused: {path} is on the deny list", file=sys.stderr)
    sys.exit(2)
row = table.get(path)
if row is None:
    print(f"refused: {path} is not on the allowlist; rootq --help names it", file=sys.stderr)
    sys.exit(2)
live = hashlib.sha256(row["content"].encode()).hexdigest()
if verb == "stat":
    size = len(row["content"].encode())
    print(f"mode={row['mode']} type=file owner={row['owner']} group={row['group']} "
          f"size={size} mtime=2026-10-25T00:00:00Z")
    sys.exit(0)
if verb == "sha256":
    print(live)
    sys.exit(0)
other = rest[1] if len(rest) > 1 else ""
if re.fullmatch(r"[0-9a-f]{64}", other):
    want = other
else:
    want = hashlib.sha256(open(other, "rb").read()).hexdigest()
print("match" if want == live else "differ")
sys.exit(0 if want == live else 1)
"""
# Runs the remote command here, so a fix that reaches the host over ssh meets the fakes.
FAKE_SSH = r"""#!@PYTHON@
import os, sys
args = sys.argv[1:]
while args and args[0].startswith("-"):
    args = args[2:] if args[0] in ("-o", "-p", "-i", "-l") else args[1:]
os.execvp("sh", ["sh", "-c", " ".join(args[1:])])
"""
FAKE_SUDO = '#!/bin/sh\necho "fake sudo refuses: $*" >&2\nexit 1\n'


def _rig(tmp_path, monkeypatch, tasks: str, live: dict[str, dict]):
    """A role writing `tasks`, its files locked away, and the fakes first on PATH.

    `tasks` names its paths through `@L@`, the locked directory. `live` maps each such path
    to the `mode`, `owner`, `group` and `content` the fake rootq reports for it.
    """
    if os.geteuid() == 0:
        pytest.skip("root traverses a 0000 directory, so no path would read as unread")
    locked = tmp_path / "locked"
    locked.mkdir()
    role = tmp_path / "x"
    for sub, name, text in (
        ("tasks", "main.yml", tasks.replace("@L@", str(locked))),
        ("templates", "x.sh.j2", SCRIPT),
        ("templates", "hook.env.j2", "WEBHOOK={{ x_webhook }}\n"),
    ):
        (role / sub).mkdir(parents=True, exist_ok=True)
        (role / sub / name).write_text(text)
    bin_dir, log = tmp_path / "bin", tmp_path / "rootq.log"
    bin_dir.mkdir()
    log.write_text("")
    table = tmp_path / "table.json"
    table.write_text(
        json.dumps({p.replace("@L@", str(locked)): row for p, row in live.items()})
    )
    for name, body in (("rootq", FAKE_ROOTQ), ("ssh", FAKE_SSH), ("sudo", FAKE_SUDO)):
        text = body.replace("@PYTHON@", sys.executable)
        text = text.replace("@LOG@", str(log)).replace("@TABLE@", str(table))
        write_exec(bin_dir / name, text)
    monkeypatch.setenv("PATH", path_with(bin_dir))
    monkeypatch.setattr(socket, "gethostname", lambda: "daniel-box")
    locked.chmod(0)
    monkeypatch.chdir(applied.ANSIBLE.parent)
    return SimpleNamespace(locked=locked, log=log, setup=tmp_path)


def _run(rig, capsys) -> tuple[int, dict[str, tuple[str, str]]]:
    """The exit code and each path's (status, row) from one run on daniel-box."""
    ns = SimpleNamespace(role="x", host="daniel-box", dry_run=False)
    try:
        code = applied.run_applied(
            ns, setup=rig.setup, render_ctx=lambda role_dir, host: CTX
        )
    finally:
        rig.locked.chmod(stat.S_IRWXU)
    rows = {}
    for line in capsys.readouterr().out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].startswith(str(rig.locked)):
            rows[parts[1]] = (parts[0], line)
    return code, rows


SCRIPT_TASK = """\
- name: Install the script
  ansible.builtin.template: {src: x.sh.j2, dest: "@L@/x.sh", mode: "0755", owner: root, group: root}
"""
ENV_TASK = """\
- name: Install the webhook env file
  ansible.builtin.template: {src: hook.env.j2, dest: "@L@/hook.env", mode: "0600", owner: root, group: root}
"""
AS_RENDERED = {"mode": "0755", "owner": "root", "group": "root", "content": RENDERED}


def test_a_root_only_file_that_matches_the_render_reads_ok_and_exits_0(
    tmp_path, monkeypatch, capsys
):
    rig = _rig(tmp_path, monkeypatch, SCRIPT_TASK, {"@L@/x.sh": AS_RENDERED})
    code, rows = _run(rig, capsys)
    assert rows[f"{rig.locked}/x.sh"][0] == "ok", rows
    assert code == PROBE_OK


@pytest.mark.parametrize(
    "change",
    [{"content": RENDERED + "# edited\n"}, {"mode": "0777"}],
    ids=["content", "mode"],  # fmt: skip
)
def test_a_root_only_file_that_differs_from_the_render_is_drift(
    tmp_path, monkeypatch, capsys, change
):
    live = {"@L@/x.sh": {**AS_RENDERED, **change}}
    rig = _rig(tmp_path, monkeypatch, SCRIPT_TASK, live)
    code, rows = _run(rig, capsys)
    assert rows[f"{rig.locked}/x.sh"][0] == "DRIFT", rows
    assert code == PROBE_FAILED


def test_a_credential_file_rootq_refuses_reads_ok_meta_with_the_reason(
    tmp_path, monkeypatch, capsys
):
    """rootq's `*.env` DENY refuses even `stat`, so no mode or owner is pinned here."""
    rig = _rig(tmp_path, monkeypatch, SCRIPT_TASK + ENV_TASK, {"@L@/x.sh": AS_RENDERED})
    code, rows = _run(rig, capsys)
    status, line = rows[f"{rig.locked}/hook.env"]
    assert status == "ok-meta", line
    assert "deny list" in line or "credential" in line, line
    assert code == PROBE_OK


def test_applied_asks_rootq_only_through_stat_and_matches(
    tmp_path, monkeypatch, capsys
):
    rig = _rig(tmp_path, monkeypatch, SCRIPT_TASK + ENV_TASK, {"@L@/x.sh": AS_RENDERED})
    _run(rig, capsys)
    verbs = [json.loads(line)[0] for line in rig.log.read_text().splitlines()]
    assert verbs, "applied never consulted rootq for the paths it could not stat"
    assert set(verbs) <= ALLOWED_VERBS, verbs


def test_a_root_only_file_rootq_does_not_list_stays_unread_with_its_reason(
    tmp_path, monkeypatch, capsys
):
    rig = _rig(tmp_path, monkeypatch, SCRIPT_TASK, {})
    code, rows = _run(rig, capsys)
    status, line = rows[f"{rig.locked}/x.sh"]
    assert status == "unread", line
    assert "not on the allowlist" in line, line
    assert code == PROBE_UNREADABLE
