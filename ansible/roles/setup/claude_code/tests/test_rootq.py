"""rootq's root script as a case table: what it refuses, what it runs, and what it prints (#6323).

The script runs in-process as an ordinary user. A test roots its allowlist under a scratch
directory in the home and points its binaries at fakes that record their argv, through the
seam the script ignores as root. Nothing runs as root. The output filter's own case table is
tests/test_rootq_redaction.py, and the shim's is tests/test_rootq_install.py.

To add a path to the allowlist, add a must-pass case here and a must-refuse neighbour: a
sibling file in the same directory that the new entry still does not admit.

Run: uv run pytest ansible/roles/setup/claude_code/tests/test_rootq.py
"""

import ast
import fnmatch
import hashlib
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
    "/root/.netrc",
    "/etc/backup.netrc",
    "/etc/wireguard/wg0.conf",
    "/etc/netplan/site.psk",
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
    # The shapes the tree renders into the hosts (#6323's census).
    "kuma-push": 'PUSH_URL="https://${KUMA_HOST}/api/push/sentinel"\n',
    "netplan-wifi": 'wifis:\n  wlan0:\n    access-points:\n      "home":\n        password: "sentinel"\n',
    "hc-ping": "curl https://hc-ping.com/0a1b2c3d-sentinel\n",
    "flag-argument": "backup --password sentinel /srv\n",
    # Credential-named keys with a literal value, in the shapes other files write them.
    "capitalised-key": "Password=sentinel\n",
    "private-key-ini": "[Interface]\nPrivateKey = sentinel\n",
    "spaced-assignment": 'TOKEN = "sentinel"\n',
    "json": '{"token": "sentinel"}\n',
    "yaml-suffixed-key": "  auth_token: sentinel\n",
    "yaml-api-key": "api_key: sentinel\n",
    "header": 'curl -H "X-Api-Key: sentinel" https://x\n',
    "lowercase-header": "x-token: sentinel\n",
    "url-query": "curl https://x/ping?key=sentinel\n",
    # Split so the fixture does not read as a real credential to gitleaks.
    "basic-auth": "curl -u admin" + ":sentinel https://x\n",
    "mysql": "mysql -psentinel db\n",
    "slack": "WEBHOOK_URL: https://hooks.slack.com/services/T0/B0/sentinel\n",
}
# One allowlisted file per PATHS entry, and its neighbour: a sibling in a directory the
# directory-wide allowlist before #6351 admitted, which the explicit list does not.
LISTED = [
    "/etc/netplan/90-static.yaml",
    "/etc/cron.d/backup",
    "/etc/ufw/user.rules",
    "/etc/ufw/user6.rules",
    "/usr/local/bin/registry-gc.sh",
    "/etc/systemd/system/claude-rc.service",
    "/etc/systemd/system/claude-rc-restart.timer",
]
UNLISTED = [
    "/etc/netplan/90-static.yml",
    "/etc/cron.d/.placeholder",
    "/etc/ufw/ufw.conf",
    "/etc/ufw/before.rules",
    "/usr/local/bin/sync-artifacts.sh",
    "/etc/systemd/system/claude-rc.service.d",
    "/etc/systemd/system/claude.slice",
    "/etc/crontab",
    "/etc/cron.daily/aide",
    "/usr/local/sbin/homelab-rootq",
]
HEX = "0" * 64


class Rig:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.log = root / "calls.log"
        self.bin.mkdir()
        self.log.write_text("")
        for argv in rootq.COMMANDS.values():
            name = os.path.basename(argv[0])
            body = FAKE.replace("@LOG@", str(self.log)).replace("@DIR@", str(self.bin))
            write_exec(self.bin / name, body)

    def env(self) -> dict[str, str]:
        return {
            "HOMELAB_ROOTQ_ROOT": str(self.root),
            "HOMELAB_ROOTQ_BIN": str(self.bin),
            "HOMELAB_ROOTQ_OWNER_UID": str(os.getuid()),
        }

    def file(self, live: str, text: str = "hello\n") -> str:
        """Write LIVE (a host path such as /etc/netplan/x.yaml) under the rig; its rig path."""
        path = Path(str(self.root) + live)
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
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
    old = os.umask(0o022)
    root = Path(tempfile.mkdtemp(prefix="rootq-test-", dir=home))
    os.chmod(root, 0o755)
    yield Rig(root)
    os.umask(old)
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


@pytest.mark.parametrize("verb", ["cat", "stat", "sha256"])
@pytest.mark.parametrize("path", DENIED)
def test_a_hard_denied_path_is_refused(rig, capsys, verb, path) -> None:
    assert_refused(run(rig, capsys, verb, path), rig, "deny list")


def test_a_hard_denied_file_matching_an_allowlist_glob_is_refused(rig, capsys) -> None:
    path = rig.file("/etc/cron.d/agent.env", "TOKEN=sentinel\n")
    assert_refused(run(rig, capsys, "stat", path), rig, "deny list")
    assert_refused(run(rig, capsys, "matches", path, HEX), rig, "deny list")


@pytest.mark.parametrize("live", UNLISTED)
@pytest.mark.parametrize("verb", ["stat", "sha256", "cat"])
def test_a_sibling_the_list_does_not_name_is_refused(rig, capsys, verb, live) -> None:
    path = (
        rig.file(live)
        if not live.endswith(".d")
        else str(Path(rig.file(live + "/x")).parent)
    )
    assert_refused(run(rig, capsys, verb, path), rig, "not on the allowlist")


@pytest.mark.parametrize(
    "live", [p for p in LISTED if not p.startswith("/etc/netplan/")]
)
def test_cat_of_a_listed_file_outside_netplan_is_refused(rig, capsys, live) -> None:
    assert_refused(run(rig, capsys, "cat", rig.file(live)), rig, "not on the allowlist")


def test_a_symlink_into_a_denied_path_is_refused(rig, capsys) -> None:
    link = Path(rig.file("/etc/netplan/x")).parent / "shadow.yaml"
    link.symlink_to("/etc/shadow")
    assert_refused(run(rig, capsys, "cat", str(link)), rig, "resolves to")


def test_a_symlink_that_stays_inside_the_list_is_refused(rig, capsys) -> None:
    target = rig.file("/etc/netplan/real.yaml")
    link = Path(target).parent / "link.yaml"
    link.symlink_to(target)
    assert_refused(run(rig, capsys, "sha256", str(link)), rig, "resolves to")


@pytest.mark.parametrize("suffix", ["sub/../a.yaml", "./a.yaml", "/a.yaml", "a.yaml/"])
def test_a_path_that_does_not_resolve_to_itself_is_refused(rig, capsys, suffix) -> None:
    rig.file("/etc/netplan/a.yaml")
    rig.file("/etc/netplan/sub/a.yaml")
    path = f"{rig.root}/etc/netplan/{suffix}"
    assert_refused(run(rig, capsys, "cat", path), rig)


def test_a_path_outside_the_allowlist_is_refused(rig, capsys) -> None:
    assert_refused(run(rig, capsys, "stat", "/etc/passwd"), rig, "not on the allowlist")
    # The real /etc/netplan is not the rig's: the list is rooted at the prefix.
    assert_refused(run(rig, capsys, "stat", str(rig.root)), rig, "not on the allowlist")


@pytest.mark.parametrize(
    "verb", ["rm", "sh", "eval", "cp", "tee", "CAT", "", "--nft", "ls", "head", "env"]
)
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
        ["stat", "-c", "%U", "/etc/netplan"],
        ["cat", "/etc/netplan", "/etc/ufw"],
        ["cat"],
        ["sha256"],
        ["matches", "/etc/netplan/a.yaml"],
        ["matches", "/etc/netplan/a.yaml", HEX, HEX],
    ],
)
def test_extra_flags_and_arguments_are_refused(rig, capsys, argv) -> None:
    assert_refused(run(rig, capsys, *argv), rig)


@pytest.mark.parametrize("digest", ["", "0" * 63, "0" * 65, "G" * 64, "network:\n  x"])
def test_matches_takes_only_a_hash_never_content(rig, capsys, digest) -> None:
    path = rig.file("/etc/netplan/a.yaml")
    assert_refused(run(rig, capsys, "matches", path, digest), rig, "64-hex")


@pytest.mark.parametrize(
    "name", ["a;id", "$(id)", "a|b", "a`id`", "a b", "a\nb", "a*", "a&b", "a>b", "~x"]
)
def test_shell_metacharacters_in_a_path_are_refused(rig, capsys, name) -> None:
    path = f"{rig.root}/etc/netplan/{name}.yaml"
    assert_refused(run(rig, capsys, "cat", path), rig, "must be absolute")


def test_a_relative_path_is_refused(rig, capsys) -> None:
    assert_refused(
        run(rig, capsys, "stat", "etc/netplan/a.yaml"), rig, "must be absolute"
    )


def test_a_group_writable_directory_above_the_path_is_refused(rig, capsys) -> None:
    path = rig.file("/etc/netplan/a.yaml")
    os.chmod(Path(path).parent, 0o775)
    assert_refused(run(rig, capsys, "stat", path), rig, "writable by root alone")


def test_a_hard_linked_file_is_refused(rig, capsys) -> None:
    path = rig.file("/etc/netplan/a.yaml")
    os.link(path, rig.root / "elsewhere")
    assert_refused(run(rig, capsys, "cat", path), rig, "2 hard links")
    assert_refused(run(rig, capsys, "sha256", path), rig, "2 hard links")


def test_a_binary_or_oversized_file_is_refused(rig, capsys) -> None:
    binary = Path(rig.file("/etc/netplan/blob.yaml"))
    binary.write_bytes(b"ELF\0\1")
    assert_refused(run(rig, capsys, "cat", str(binary)), rig, "binary")
    big = rig.file("/etc/netplan/big.yaml", "x" * (rootq.READ_MAX + 1))
    assert_refused(run(rig, capsys, "cat", big), rig, "over")
    folder = Path(rig.file("/etc/netplan/dir.yaml/x")).parent
    assert_refused(run(rig, capsys, "cat", str(folder)), rig, "not a regular file")


@pytest.mark.parametrize("kind", sorted(SECRETS))
def test_a_file_holding_a_credential_is_withheld_whole(rig, capsys, kind) -> None:
    path = rig.file("/etc/netplan/a.yaml", "network:\n  version: 2\n" + SECRETS[kind])
    assert_refused(run(rig, capsys, "cat", path), rig, "credential pattern")


@pytest.mark.parametrize("verb", ["sha256", "matches"])
def test_a_file_holding_a_credential_is_not_hashed(rig, capsys, verb) -> None:
    path = rig.file("/etc/netplan/wifi.yaml", SECRETS["netplan-wifi"])
    argv = [verb, path, HEX] if verb == "matches" else [verb, path]
    assert_refused(run(rig, capsys, *argv), rig, "credential pattern")


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


def test_the_verbs_are_exactly_the_fixed_set() -> None:
    assert rootq.COMMANDS == {
        "nft": ["/usr/sbin/nft", "list", "ruleset"],
        "ufw": ["/usr/sbin/ufw", "status", "verbose"],
        "sshd": ["/usr/sbin/sshd", "-T"],
        "crontab": ["/usr/bin/crontab", "-l", "-u", "root"],
    }
    assert rootq.TIER1 == ("stat", "sha256", "matches")
    assert rootq.CAT == (("/etc/netplan", "*.yaml"),)


@pytest.mark.parametrize("live", LISTED)
def test_every_listed_path_is_admitted(rig, capsys, live) -> None:
    rc, out, _ = run(rig, capsys, "sha256", rig.file(live))
    assert (rc, out) == (0, hashlib.sha256(b"hello\n").hexdigest() + "\n")


# The root-only files `probe.py applied claude_code` read as `unread` on 2026-10-25 that
# PATHS does not yet admit (#6357). The neighbour is a polkit rule the role does not install.
POLKIT_RULES = [
    "/etc/polkit-1/rules.d/50-claude-fanout-alert.rules",
    "/etc/polkit-1/rules.d/50-claude-land.rules",
    "/etc/polkit-1/rules.d/50-claude-triage.rules",
]


@pytest.mark.parametrize("live", POLKIT_RULES)
def test_a_claude_polkit_rule_is_admitted_and_its_neighbour_is_not(rig, capsys, live):
    rc, out, _ = run(rig, capsys, "sha256", rig.file(live))
    assert (rc, out) == (0, hashlib.sha256(b"hello\n").hexdigest() + "\n")
    neighbour = rig.file("/etc/polkit-1/rules.d/49-ubuntu-admin.rules")
    assert_refused(run(rig, capsys, "sha256", neighbour), rig, "not on the allowlist")


@pytest.mark.parametrize(
    "verb, output, logged",
    [
        ("nft", "table inet filter {\n", "nft list ruleset"),
        ("ufw", "Status: active\n", "ufw status verbose"),
        ("sshd", "port 22\n", "sshd -T"),
        ("crontab", "SHELL=/bin/sh\n", "crontab -l -u root"),
    ],
)
def test_each_command_verb_runs_its_fixed_argv(
    rig, capsys, verb, output, logged
) -> None:
    rig.fake_output(logged.split()[0], output)
    rc, out, _ = run(rig, capsys, verb)
    assert rc == 0
    assert out == output
    assert rig.calls() == [logged]


def test_cat_prints_netplan_without_running_anything(rig, capsys) -> None:
    path = rig.file("/etc/netplan/a.yaml", "network:\n  version: 2\n")
    rc, out, _ = run(rig, capsys, "cat", path)
    assert (rc, out) == (0, "network:\n  version: 2\n")
    assert rig.calls() == []


def test_stat_prints_metadata_and_no_content(rig, capsys) -> None:
    path = rig.file("/etc/cron.d/backup", "0 * * * * root /usr/bin/sentinel\n")
    os.chmod(path, 0o640)
    rc, out, _ = run(rig, capsys, "stat", path)
    assert rc == 0
    fields = dict(f.split("=", 1) for f in out.split())
    assert fields["mode"] == "0640"
    assert (fields["type"], fields["size"]) == ("file", "33")
    assert set(fields) == {"mode", "type", "owner", "group", "size", "mtime"}
    assert "sentinel" not in out


def test_matches_compares_the_hash_and_prints_no_content(rig, capsys) -> None:
    path = rig.file("/usr/local/bin/registry-gc.sh", "#!/bin/sh\necho sentinel\n")
    same = hashlib.sha256(b"#!/bin/sh\necho sentinel\n").hexdigest()
    assert run(rig, capsys, "matches", path, same) == (0, "match\n", "")
    assert run(rig, capsys, "matches", path, HEX) == (1, "differ\n", "")
    assert rig.calls() == []


def test_a_reference_names_a_secret_without_holding_it(rig, capsys) -> None:
    rig.fake_output(
        "crontab",
        '0 * * * * curl "https://kuma/api/push/${PUSH_TOKEN}"\n'
        "0 1 * * * backup --password $PASS /srv\n# Usage: K3S_TOKEN=xxx k3s-install.sh\n",
    )
    rc, out, _ = run(rig, capsys, "crontab")
    assert rc == 0
    assert out.splitlines() == [
        "0 * * * * curl <arg>",
        "0 1 * * * backup --password <arg> /srv",
        "# <comment>",
    ]


def test_output_stops_at_the_cap(rig, capsys) -> None:
    rig.fake_output("nft", "accept " * (rootq.OUT_MAX // 7 + 10))
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
    assert all(f"rootq {v}" in out for v in [*rootq.TIER1, *rootq.COMMANDS, "cat"])
    assert rig.calls() == []


def test_as_root_the_test_seam_is_ignored() -> None:
    seam = {
        "HOMELAB_ROOTQ_ROOT": "/tmp/evil",
        "HOMELAB_ROOTQ_BIN": "/tmp/evil",
        "HOMELAB_ROOTQ_OWNER_UID": "1000",
    }
    assert rootq.config(seam, euid=0) == ("", "", frozenset({0}))
    assert rootq.config(seam, euid=1000) == (
        "/tmp/evil",
        "/tmp/evil",
        frozenset({0, 1000}),
    )


def test_the_root_script_parses_with_the_3_12_grammar() -> None:
    # The hosts run it under Ubuntu 24.04's python3, so a 3.14-only construct would fail every
    # run there and none here.
    ast.parse(Path(rootq.__file__).read_text(), feature_version=(3, 12))
