"""The one-time uid move for host-package users (#8100): its dry run, its apply and its refusals.

Run: uv run pytest scripts/deploy_tools/tests/test_renumber_host_users.py
"""

import json
import subprocess

import pytest

import renumber_host_users as script
from lib.exit_codes import FAILED

DECLARED = [
    {"name": "claude", "uid": 900, "home": "/var/lib/claude"},
    {"name": "builder", "uid": 902, "home": "/var/lib/builder"},
    {"name": "absent", "uid": 903, "home": "/var/lib/absent"},
]
PASSWD = (
    "root:x:0:0::/root:/bin/bash\n"
    "claude:x:995:995::/var/lib/claude:/bin/bash\n"
    "builder:x:902:902::/var/lib/builder:/usr/sbin/nologin\n"
)
GROUP = "root:x:0:\nclaude:x:995:\nbuilder:x:902:\n"
DIRS = {"/var/lib/claude", "/tmp"}
SERVICES = "user@995.service\nuser@995.service\nclaude-rc.service\nsession-3.scope\n"


class Host:
    """A fake host: its passwd and group files, and a runner that records each command."""

    def __init__(
        self, tmp_path, passwd=PASSWD, ps_output="user@995.service\nclaude-rc.service\n"
    ):
        (tmp_path / "manifest.json").write_text(
            json.dumps({"users": {"users": DECLARED}})
        )
        (tmp_path / "passwd").write_text(passwd)
        (tmp_path / "group").write_text(GROUP)
        self.dir, self.ps_output, self.calls = tmp_path, ps_output, []

    def run(self, argv):
        self.calls.append(list(argv))
        if argv[0] == "ps":
            return subprocess.CompletedProcess(argv, 0, self.ps_output, "")
        if argv[0] == "usermod":
            passwd = self.dir / "passwd"
            passwd.write_text(
                passwd.read_text().replace(":995:995:", f":{argv[2]}:995:")
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    def main(self, *args, euid=0):
        return script.main(
            list(args),
            euid=lambda: euid,
            run=self.run,
            manifest=self.dir / "manifest.json",
            passwd=self.dir / "passwd",
            group=self.dir / "group",
            exists=lambda path: path in DIRS,
            host="daniel-box",
        )

    def changes(self):
        return [c for c in self.calls if c[0] != "ps"]


def test_a_dry_run_prints_each_move_and_its_commands_and_changes_nothing(
    tmp_path, capsys
):
    host = Host(tmp_path)

    assert host.main() == 0

    out = capsys.readouterr().out
    assert out.splitlines()[0] == (
        "daniel-box: 3 declared users: 1 move, 0 blocked, 1 ok, 1 absent (dry run, --apply to run)"
    )
    assert "claude: uid 995 -> 900 (holder of 900: none)" in out
    assert (
        "    systemctl stop claude-rc.service\n    systemctl stop user@995.service\n"
        in out
    )
    assert "    usermod -u 900 claude\n    groupmod -g 900 claude\n" in out
    assert "find /var/lib/claude /tmp -xdev -uid 995 -exec chown -h 900 '{}' +" in out
    assert "find /var/lib/claude /tmp -xdev -gid 995 -exec chown -h :900 '{}' +" in out
    assert "    systemctl start user@900.service" in out
    assert "builder" not in out.split("\n", 1)[1], (
        "a user at its uid is counted, not listed"
    )
    assert host.changes() == []


def test_apply_moves_the_user_once_and_a_second_run_does_nothing(tmp_path, capsys):
    host = Host(tmp_path)

    assert host.main("--apply") == 0

    assert host.changes() == [
        ["systemctl", "stop", "claude-rc.service"],
        ["systemctl", "stop", "user@995.service"],
        ["usermod", "-u", "900", "claude"],
        ["groupmod", "-g", "900", "claude"],
        [
            "find",
            "/var/lib/claude",
            "/tmp",
            "-xdev",
            "-uid",
            "995",
            "-exec",
            "chown",
            "-h",
            "900",
            "{}",
            "+",
        ],
        [
            "find",
            "/var/lib/claude",
            "/tmp",
            "-xdev",
            "-gid",
            "995",
            "-exec",
            "chown",
            "-h",
            ":900",
            "{}",
            "+",
        ],
        ["systemctl", "start", "claude-rc.service"],
        ["systemctl", "start", "user@900.service"],
    ]
    host.calls.clear()
    capsys.readouterr()

    assert host.main("--apply") == 0

    assert host.calls == [], (
        "every user is at its declared uid, so nothing runs, not even ps"
    )
    assert "0 move, 0 blocked, 2 ok, 1 absent" in capsys.readouterr().out


def test_a_uid_another_account_holds_is_named_and_left_alone(tmp_path, capsys):
    host = Host(
        tmp_path, passwd=PASSWD + "squatter:x:900:900::/nonexistent:/usr/sbin/nologin\n"
    )

    assert host.main("--apply") == 1

    out = capsys.readouterr().out
    assert "claude: uid 995 -> 900 (holder of 900: squatter)" in out
    assert "left alone: squatter holds uid 900" in out
    assert host.changes() == []


def test_a_login_session_leaves_the_user_alone_because_usermod_refuses_it(
    tmp_path, capsys
):
    host = Host(tmp_path, ps_output=SERVICES)

    assert host.main("--apply") == 1

    assert "left alone: session-3.scope runs as it" in capsys.readouterr().out
    assert host.changes() == []


@pytest.mark.parametrize("args", [(), ("--apply",)])
def test_without_root_it_refuses_and_runs_nothing(tmp_path, capsys, args):
    host = Host(tmp_path)

    assert host.main(*args, euid=1000) == FAILED

    assert "must run as root" in capsys.readouterr().err
    assert host.calls == []
