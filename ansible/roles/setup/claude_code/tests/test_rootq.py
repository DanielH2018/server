"""rootq's root script as a case table: what it refuses, what it runs, and what it prints (#6323).

The script runs in-process as an ordinary user. A test points its ALLOW roots at a scratch
directory under the home and its binaries at fakes that record their argv, through the seam
the script ignores as root. Nothing runs as root.

Run: uv run pytest ansible/roles/setup/claude_code/tests/test_rootq.py
"""

import fnmatch
import os
import shutil
import tempfile
from pathlib import Path

import homelab_rootq as rootq
import pytest
from lib.proc_testing import write_exec

FAKE = """#!/usr/bin/bash
echo "$(basename "$0") $*" >> "@LOG@"
[ -e "@DIR@/$(basename "$0").out" ] && cat "@DIR@/$(basename "$0").out"
exit "$(cat "@DIR@/$(basename "$0").rc" 2> /dev/null || echo 0)"
"""

# One concrete path per DENY pattern; test_every_deny_pattern_has_a_case holds the two together.
DENIED = [
    "/etc/shadow",
    "/etc/shadow-",
    "/etc/gshadow",
    "/etc/sudoers",
    "/etc/sudoers.d/claude-rootq",
    "/root/.ssh/authorized_keys",
    "/home/ubuntu/.ssh/id_ed25519",
    "/etc/ssh/ssh_host_ed25519_key",
    "/etc/claude-rc/session.env",
    "/etc/rancher/k3s/k3s.yaml",
    "/var/lib/rancher/k3s/server/token",
    "/var/lib/rancher/k3s/server/node-token",
    "/var/lib/rancher/k3s/server/cred/passwd",
    "/opt/app/credentials/db",
    "/home/ubuntu/.config/sops/age/keys.txt",
    "/etc/backup.agekey",
    "/home/claude/.config/sops/age",
    "/var/lib/kubelet/pods/0a1b/volumes/kubernetes.io~csi/pvc-1/mount",
]
SECRETS = {
    "private-key": "-----BEGIN OPENSSH PRIVATE KEY-----\nsentinel\n",
    "age-key": "AGE-SECRET-KEY-1SENTINEL\n",
    "sops": "token: ENC[AES256_GCM,data:sentinel]\n",
    "authorization": "curl -H 'Authorization: sentinel' https://x\n",
    "bearer": "curl -H 'X: Bearer sentinel' https://x\n",
    "webhook": "curl https://discord.com/api/webhooks/1/sentinel\n",
    "userinfo": "rsync rsync://backup:sentinel@nas/share\n",
    "k3s-token": "K10" + "a" * 20 + "::server:sentinel\n",
    "github": "GH=ghp_" + "S" * 30 + "\n",
}


class Rig:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.allow = root / "allow"
        self.bin = root / "bin"
        self.log = root / "calls.log"
        self.allow.mkdir(mode=0o755)
        self.bin.mkdir()
        self.log.write_text("")
        for argv in [*rootq.COMMANDS.values(), *rootq.PATH_COMMANDS.values()]:
            name = os.path.basename(argv[0])
            body = FAKE.replace("@LOG@", str(self.log)).replace("@DIR@", str(self.bin))
            write_exec(self.bin / name, body)

    def env(self) -> dict[str, str]:
        return {
            "HOMELAB_ROOTQ_ALLOW": str(self.allow),
            "HOMELAB_ROOTQ_BIN": str(self.bin),
            "HOMELAB_ROOTQ_OWNER_UID": str(os.getuid()),
        }

    def file(self, name: str, text: str = "hello\n") -> str:
        path = self.allow / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return str(path)

    def fake_output(self, binary: str, text: str, rc: int = 0) -> None:
        (self.bin / f"{binary}.out").write_text(text)
        (self.bin / f"{binary}.rc").write_text(str(rc))

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()


@pytest.fixture
def rig():
    """A scratch tree under the real home, whose ancestors are root's or this user's alone."""
    home = Path(os.path.realpath(Path.home()))
    if not os.access(home, os.W_OK) or any(
        Path(p).stat().st_mode & 0o022 for p in [home, *home.parents]
    ):
        pytest.skip("the home or a parent of it is group- or world-writable")
    root = Path(tempfile.mkdtemp(prefix="rootq-test-", dir=home))
    os.chmod(root, 0o755)
    yield Rig(root)
    shutil.rmtree(root, ignore_errors=True)


def run(rig: Rig, capsys, *argv: str) -> tuple[int, str, str]:
    rc = rootq.main(list(argv), rig.env())
    out, err = capsys.readouterr()
    return rc, out, err


def assert_refused(result: tuple[int, str, str], rig: Rig, reason: str = "") -> None:
    rc, out, err = result
    assert rc == 2, (out, err)
    assert out == ""
    assert err.startswith("refused:")
    assert reason in err
    assert "sentinel" not in err
    assert rig.calls() == []


# ── Must refuse ───────────────────────────────────────────────────────────────────────────


def test_every_deny_pattern_has_a_case() -> None:
    for pattern in rootq.DENY:
        assert any(
            fnmatch.fnmatchcase(a, pattern)
            for p in DENIED
            for a in [p, *map(str, Path(p).parents)]
        ), pattern


@pytest.mark.parametrize("verb", ["cat", "ls", "stat"])
@pytest.mark.parametrize("path", DENIED)
def test_a_hard_denied_path_is_refused(rig, capsys, verb, path) -> None:
    assert_refused(run(rig, capsys, verb, path), rig, "deny list")


def test_a_hard_denied_file_inside_an_allowed_root_is_refused(rig, capsys) -> None:
    path = rig.file("agent.env", "TOKEN=sentinel\n")
    assert_refused(run(rig, capsys, "cat", path), rig, "deny list")
    assert_refused(run(rig, capsys, "ls", path), rig, "deny list")


def test_a_symlink_into_a_denied_path_is_refused(rig, capsys) -> None:
    (rig.allow / "shadow").symlink_to("/etc/shadow")
    assert_refused(
        run(rig, capsys, "cat", str(rig.allow / "shadow")), rig, "resolves to"
    )


def test_a_symlink_that_stays_inside_the_root_is_refused(rig, capsys) -> None:
    target = rig.file("real")
    (rig.allow / "link").symlink_to(target)
    assert_refused(run(rig, capsys, "cat", str(rig.allow / "link")), rig, "resolves to")


@pytest.mark.parametrize("suffix", ["sub/../real", "./real", "sub//real", "sub/"])
def test_a_path_that_does_not_resolve_to_itself_is_refused(rig, capsys, suffix) -> None:
    rig.file("real")
    rig.file("sub/real")
    assert_refused(run(rig, capsys, "cat", f"{rig.allow}/{suffix}"), rig, "resolves to")


def test_a_path_outside_the_allowlist_is_refused(rig, capsys) -> None:
    assert_refused(run(rig, capsys, "cat", "/etc/passwd"), rig, "under none of")
    assert_refused(run(rig, capsys, "ls", str(rig.root)), rig, "under none of")


@pytest.mark.parametrize("verb", ["rm", "sh", "eval", "cp", "tee", "CAT", "", "--nft"])
def test_an_unknown_verb_is_refused(rig, capsys, verb) -> None:
    assert_refused(run(rig, capsys, verb), rig, "unknown verb")


@pytest.mark.parametrize(
    "argv",
    [
        ["nft", "--json"],
        ["nft", "list", "ruleset"],
        ["ufw", "numbered"],
        ["sshd", "-C", "user=x"],
        ["crontab", "-u", "ubuntu"],
        ["ls", "-R", "/etc/netplan"],
        ["stat", "-c", "%U", "/etc/netplan"],
        ["cat", "/etc/netplan", "/etc/ufw"],
        ["cat"],
    ],
)
def test_extra_flags_and_arguments_are_refused(rig, capsys, argv) -> None:
    assert_refused(run(rig, capsys, *argv), rig)


@pytest.mark.parametrize(
    "name", ["a;id", "$(id)", "a|b", "a`id`", "a b", "a\nb", "a*", "a&b", "a>b", "~x"]
)
def test_shell_metacharacters_in_a_path_are_refused(rig, capsys, name) -> None:
    assert_refused(
        run(rig, capsys, "cat", f"{rig.allow}/{name}"), rig, "must be absolute"
    )


def test_a_relative_path_is_refused(rig, capsys) -> None:
    assert_refused(run(rig, capsys, "ls", "etc/netplan"), rig, "must be absolute")


def test_a_group_writable_directory_above_the_path_is_refused(rig, capsys) -> None:
    path = rig.file("loose/file")
    os.chmod(rig.allow / "loose", 0o775)
    assert_refused(run(rig, capsys, "cat", path), rig, "writable by root alone")


def test_a_hard_linked_file_is_refused(rig, capsys) -> None:
    path = rig.file("real")
    os.link(path, rig.root / "elsewhere")
    assert_refused(run(rig, capsys, "cat", path), rig, "2 hard links")


def test_a_binary_or_oversized_file_is_refused(rig, capsys) -> None:
    binary = rig.allow / "blob"
    binary.write_bytes(b"ELF\0\1")
    assert_refused(run(rig, capsys, "cat", str(binary)), rig, "binary")
    big = rig.file("big", "x" * (rootq.READ_MAX + 1))
    assert_refused(run(rig, capsys, "cat", big), rig, "over")
    assert_refused(run(rig, capsys, "cat", str(rig.allow)), rig, "not a regular file")


@pytest.mark.parametrize("kind", sorted(SECRETS))
def test_a_file_holding_a_credential_is_withheld_whole(rig, capsys, kind) -> None:
    path = rig.file("script.sh", "#!/bin/sh\necho harmless\n" + SECRETS[kind])
    assert_refused(run(rig, capsys, "cat", path), rig, "credential pattern")


def test_a_command_whose_output_holds_a_credential_is_withheld_whole(
    rig, capsys
) -> None:
    rig.fake_output("crontab", "0 * * * * " + SECRETS["webhook"])
    rc, out, err = run(rig, capsys, "crontab")
    assert rc == 2
    assert out == ""
    assert "credential pattern" in err
    assert "sentinel" not in out + err


# ── Must pass, with the argv each verb executes pinned ────────────────────────────────────


def test_the_commands_are_exactly_the_fixed_set() -> None:
    assert rootq.COMMANDS == {
        "nft": ["/usr/sbin/nft", "list", "ruleset"],
        "ufw": ["/usr/sbin/ufw", "status", "verbose"],
        "sshd": ["/usr/sbin/sshd", "-T"],
        "crontab": ["/usr/bin/crontab", "-l", "-u", "root"],
    }
    assert rootq.PATH_COMMANDS == {
        "ls": ["/usr/bin/ls", "-la", "--"],
        "stat": ["/usr/bin/stat", "--"],
    }
    assert rootq.PATH_VERBS == ("ls", "stat", "cat")


@pytest.mark.parametrize(
    "verb, logged",
    [
        ("nft", "nft list ruleset"),
        ("ufw", "ufw status verbose"),
        ("sshd", "sshd -T"),
        ("crontab", "crontab -l -u root"),
    ],
)
def test_each_command_verb_runs_its_fixed_argv(rig, capsys, verb, logged) -> None:
    rig.fake_output(logged.split()[0], "state\n")
    rc, out, _ = run(rig, capsys, verb)
    assert rc == 0
    assert out == "state\n"
    assert rig.calls() == [logged]


@pytest.mark.parametrize("verb, flags", [("ls", "-la --"), ("stat", "--")])
def test_each_path_verb_runs_its_fixed_argv(rig, capsys, verb, flags) -> None:
    rc, _, _ = run(rig, capsys, verb, str(rig.allow))
    assert rc == 0
    assert rig.calls() == [f"{verb} {flags} {rig.allow}"]


def test_cat_prints_an_allowed_file_without_running_anything(rig, capsys) -> None:
    path = rig.file("netplan.yaml", "network:\n  version: 2\n")
    rc, out, _ = run(rig, capsys, "cat", path)
    assert (rc, out) == (0, "network:\n  version: 2\n")
    assert rig.calls() == []


def test_assignment_values_are_redacted_unless_plain(rig, capsys) -> None:
    rig.fake_output(
        "crontab",
        "MAILTO=root\nPATH=/usr/local/bin:/usr/bin\nTIMEOUT=30\nRUN=yes\nLAG=5min\n"
        "API=sentinel-1\n0 * * * * curl https://x/ping?key=sentinel-2 >/dev/null\n",
    )
    rc, out, _ = run(rig, capsys, "crontab")
    assert rc == 0
    assert out.splitlines() == [
        "MAILTO=<value>",
        "PATH=/usr/local/bin:/usr/bin",
        "TIMEOUT=30",
        "RUN=yes",
        "LAG=5min",
        "API=<value>",
        "0 * * * * curl https://x/ping?key=<value> >/dev/null",
    ]


def test_a_unit_file_keeps_its_directives_and_redacts_its_environment(
    rig, capsys
) -> None:
    path = rig.file(
        "demo.service",
        '[Service]\nType=oneshot\nUser=claude\nEnvironment="API_KEY=sentinel" N=3 '
        'HOME=/root\nEnvironment="BROKEN=sentinel\nExecStart=/usr/bin/x --token=sentinel\n',
    )
    rc, out, _ = run(rig, capsys, "cat", path)
    assert rc == 0
    assert out.splitlines() == [
        "[Service]",
        "Type=oneshot",
        "User=claude",
        "Environment='API_KEY=<value>' N=3 HOME=/root",
        "Environment=<value>",
        "ExecStart=/usr/bin/x --token=<value>",
    ]


def test_output_stops_at_the_cap(rig, capsys) -> None:
    rig.fake_output("nft", "x" * (rootq.OUT_MAX + 10))
    rc, out, _ = run(rig, capsys, "nft")
    assert rc == 0
    assert out.endswith(f"\n[truncated at {rootq.OUT_MAX} bytes]\n")
    assert len(out) < rootq.OUT_MAX + 64


def test_a_failed_command_exits_1_and_a_missing_one_exits_3(rig, capsys) -> None:
    rig.fake_output("crontab", "", rc=1)
    rc, _, err = run(rig, capsys, "crontab")
    assert rc == 1
    assert "exited 1" in err
    (rig.bin / "ufw").unlink()
    rc, _, err = run(rig, capsys, "ufw")
    assert rc == 3
    assert "missing command" in err


def test_help_lists_the_verbs_and_runs_nothing(rig, capsys) -> None:
    rc, out, _ = run(rig, capsys, "--help")
    assert rc == 0
    assert all(f"rootq {verb}" in out for verb in [*rootq.COMMANDS, *rootq.PATH_VERBS])
    assert rig.calls() == []


def test_as_root_the_test_seam_is_ignored() -> None:
    seam = {
        "HOMELAB_ROOTQ_ALLOW": "/",
        "HOMELAB_ROOTQ_BIN": "/tmp/evil",
        "HOMELAB_ROOTQ_OWNER_UID": "1000",
    }
    assert rootq.config(seam, euid=0) == (rootq.ALLOW, "", frozenset({0}))
    assert rootq.config(seam, euid=1000) == (("/",), "/tmp/evil", frozenset({0, 1000}))
