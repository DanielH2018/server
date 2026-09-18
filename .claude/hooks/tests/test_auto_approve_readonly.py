#!/usr/bin/env python3
"""Table-driven tests for the auto-approve-readonly Bash classifier.

The classifier is a security boundary: it may only ever REDUCE permission
prompts for *provably* read-only commands, and must NEVER auto-approve a
command that can write, delete, or execute. These tables lock that contract.

Run: uv run pytest .claude/hooks
(Still runnable standalone -- it loads the hook by path; pytest is the one dependency.)

Each table is two lists. The `_SSH` half holds every vector with an `ssh` stage; its verdict
runs through `SSH_HOSTS` and `_SSH_SECRET`, which `_readonly_tables.py` imports from the
deployed `claude_guard` package. In GitHub CI that package is `conftest.py`'s in-process
stand-in, a copy by construction, so a pass there proves nothing about the deployed tables.
The `_SSH` tests skip under the stand-in rather than pass on the copy, which is what makes
CI's own report show the hole. The `_LOCAL` half never touches those two values and runs
everywhere. The split is broad on purpose: `ssh -L … daniel-server uptime` rejects on
`_SSH_FLAGS`, a table this repo owns, but a vector left in `_LOCAL` that turns out to depend
on the host set would pass silently on stand-in data, which is the exact hole being closed.
`test_every_ssh_vector_is_in_an_ssh_list` holds the line.
"""

import importlib.util
import os
import re
import sys

import pytest


_HOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "auto-approve-readonly.py",
)
sys.path.insert(
    0, os.path.dirname(_HOOK)
)  # auto-approve-readonly.py imports _hook_common
_spec = importlib.util.spec_from_file_location("auto_approve_readonly", _HOOK)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
classify = _mod.classify


_STAND_IN = getattr(sys.modules.get("claude_guard"), "__claude_guard_stand_in__", False)
needs_deployed_tables = pytest.mark.skipif(
    _STAND_IN,
    reason="claude_guard is conftest.py's stand-in, not the deployed package; an ssh "
    "verdict against the copy proves nothing about the tables the hook runs with",
)

# A vector with an `ssh` token anywhere: as argv[0] of any stage, or as a bare `ssh -i`.
_HAS_SSH = re.compile(r"(^|\s)ssh(\s|$)")

# MUST auto-approve: provably read-only. No ssh stage in this list.
APPROVE_LOCAL = [
    ("ls", "bare ls"),
    ("cat foo.txt", "cat a file"),
    ("git status", "git read-only subcommand"),
    ("git -C /srv log --oneline", "git -C read-only"),
    ("rg pattern src/", "ripgrep search"),
    ("docker ps", "docker read-only"),
    ("docker logs web", "docker logs"),
    ("find . -name '*.yml'", "find without write actions"),
    ("cat a.txt | grep foo | head -5", "pure read-only pipeline"),
    ("pwd", "pwd builtin"),
    ("cd /home/ubuntu/server", "cd changes cwd only"),
    ("cd /srv && ls", "cd then ls"),
    ("cat a; cat b", "two reads joined by ;"),
    ("echo hi; ls; pwd", "three reads joined by ;"),
    ("ls && cat foo", "&& sequence"),
    ("false || ls", "|| sequence"),
    ("cat a\ncat b", "two reads on separate lines"),
    ("echo '=== a ==='\ncat a\necho '=== b ==='\ncat b", "header/cat blocks"),
    ("cat .yamllint 2>/dev/null", "stderr to /dev/null"),
    ("ls >/dev/null", "stdout to /dev/null"),
    ("docker ps 2>&1", "fd duplication 2>&1"),
    ("grep -r foo . 2>/dev/null | head", "redirect inside pipeline"),
    ("awk '{ print length, FILENAME }' file", "awk print length"),
    ("awk -F: '{print $1}' /etc/passwd", "awk with -F field sep"),
    ("ls | awk '{print $9}'", "awk in a pipeline"),
    ("awk 'NR==1' file", "awk line selection"),
    ("sed -n '1,5p' file", "sed print range"),
    ("sed 's/foo/bar/' file", "sed substitution to stdout"),
    ("echo x | sed 's/x/y/'", "sed in a pipeline"),
    (
        r"sed -E 's/(public key:|private key:).*/\1 [redacted]/'",
        "the wireguard redaction sed",
    ),
    ("grep foo file >/dev/null 2>&1", "combined >/dev/null 2>&1"),
    ('echo "a; rm b"', "operators inside quotes are data, not syntax"),
    ('echo "x && y | z"', "quoted pipe/and is data"),
    (
        "cd /home/ubuntu/server\n"
        'echo "=== .ansible-lint ==="; cat .ansible-lint\n'
        'echo ""; cat .yamllint 2>/dev/null\n'
        "awk '{ print length, FILENAME }' ansible/roles/containers/x/tasks/main.yml",
        "full multi-line exploration command",
    ),
    ("lsb_release -d", "lsb_release describe"),
    ("lsb_release -a", "lsb_release all"),
    ("mailq", "mail queue listing"),
    ("dpkg-query -L docker-ce", "dpkg-query list files (pure query tool)"),
    ("dpkg -l", "dpkg list installed"),
    ("dpkg -l docker-ce", "dpkg list one package"),
    ("dpkg -L docker-ce", "dpkg list a package's files"),
    ("dpkg -s docker-ce", "dpkg show package status"),
    ("dpkg -S /usr/bin/docker", "dpkg search which package owns a path"),
    ("dpkg -l | grep docker", "dpkg piped to grep"),
    ("apt list --installed", "apt list installed"),
    ("apt show jq", "apt show a package"),
    ("apt policy docker-ce", "apt policy"),
    ("apt search ansible", "apt search"),
    ("apt-mark showmanual", "apt-mark show manually-installed"),
    ("apt-mark showauto | sort", "apt-mark show auto in a pipeline"),
    ("pipx list", "pipx list"),
    ("pipx list --short", "pipx list short"),
    ("pipx environment", "pipx environment"),
    ("pipx --version", "pipx version"),
    ("crontab -l", "crontab list"),
    ("crontab -u ubuntu -l", "crontab list for a user"),
    ("sensors", "sensors read"),
    ("sensors -f", "sensors in fahrenheit"),
    ("ss -tlnp", "ss lists sockets"),
    ("dmesg", "dmesg reads the ring buffer (#2052)"),
    ("dmesg -T --level=err", "dmesg with read-only flags"),
    ("dmesg --color=never", "dmesg --color: a long option holding a c is not -c"),
    ("ping -c 1 10.0.0.161", "ping is read-only under any argument (#1898)"),
    ("traceroute 10.0.0.161", "traceroute (#1898)"),
]

# MUST auto-approve, and the verdict runs through the claude_guard tables.
APPROVE_SSH = [
    ("ssh daniel-server docker ps", "bare remote read-only command"),
    ("ssh daniel-pi uptime", "the other homelab host"),
    ("ssh ubuntu@daniel-server hostname", "user@host form"),
    ("ssh daniel-server 'docker ps | head -3'", "pipeline inside the remote string"),
    ("ssh daniel-server docker ps | head -3", "pipeline on the local side"),
    (
        "ssh daniel-server docker logs monitor-bridge --since 3h 2>&1 | tail -12",
        "remote logs with a 2>&1 dup and a local filter",
    ),
    (
        "ssh -i /home/ubuntu/.ssh/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes "
        "daniel-server git -C /home/ubuntu/server log --oneline -1",
        "the option prefix these calls are actually written with",
    ),
    ("ssh -o BatchMode=yes -o ConnectTimeout=8 daniel-pi hostname", "connect options"),
    (
        "ssh -q -p 22 daniel-server systemctl status traefik",
        "-q/-p plus a guarded verb",
    ),
    (
        "ssh daniel-server 'cd /home/ubuntu/server; git status'",
        "; sequence, both stages read-only",
    ),
]

# MUST NOT auto-approve: can write, delete, or execute (or unparseable).
REJECT_LOCAL = [
    ("rm -rf /tmp/x", "rm deletes"),
    ("git push", "git push mutates"),
    ("docker run alpine", "docker run executes"),
    ("echo $(whoami)", "command substitution $()"),
    ("cat `whoami`", "backtick substitution"),
    ("echo ${HOME}", "${ } expansion rejected by design"),
    ("tee out.txt", "tee writes"),
    ("dd if=/dev/zero of=f", "dd writes"),
    ("mv a b", "mv renames"),
    ("python3 script.py", "interpreter executes arbitrary code"),
    ("ls > out.txt", "redirect writes a real file"),
    ("cat a >> log.txt", "append writes a real file"),
    ("ls &", "backgrounding"),
    ("(cat a)", "subshell"),
    ("cat a; rm b", "one bad stage in a ; sequence"),
    ("ls && rm -rf x", "bad stage after &&"),
    ("cat a | tee out", "tee write inside pipeline"),
    ("cat a && echo $(rm x)", "substitution hidden after &&"),
    ("cat a\nrm b", "bad stage on a second line"),
    ("awk 'BEGIN{system(\"rm -rf x\")}'", "awk system() executes"),
    ("awk '{print > \"out.txt\"}' file", "awk redirects to a file"),
    ("awk '{print | \"sh\"}' file", "awk pipes to a command"),
    ("awk 'BEGIN{while((\"ls\"|getline l)>0) print l}'", "awk getline from command"),
    ("awk -f prog.awk file", "awk -f program file (uninspectable)"),
    ("gawk -i inplace '{print}' file", "gawk -i inplace edits files"),
    ("awk '{print}' > out.txt", "shell redirect to file after a safe awk"),
    ("diff <(ls) <(ls)", "process substitution"),
    ("cat a|rm b", "no-space pipe into a mutator"),
    (">/dev/null", "redirect with no command"),
    ("sed '/foo/w out' file", "sed w command reached via address"),
    ("cat a > b 2>/dev/null", "real-file write alongside a safe redirect"),
    ("sed -i 's/a/b/' file", "sed -i edits in place"),
    ("sed 's/a/b/w out.txt' file", "sed s///w writes a file"),
    ("sed 's/a/b/e' file", "sed s///e executes"),
    ("sed -n 'w out.txt' file", "sed w command writes"),
    ("sed '1e cat /etc/shadow' file", "sed e command executes"),
    ("sed -f script.sed file", "sed -f program file (uninspectable)"),
    ("dpkg", "bare dpkg has no read action -> not provably read-only"),
    ("dpkg -i pkg.deb", "dpkg -i installs"),
    ("dpkg --install pkg.deb", "dpkg --install installs"),
    ("dpkg -r docker-ce", "dpkg -r removes"),
    ("dpkg -P docker-ce", "dpkg -P purges"),
    ("dpkg --configure -a", "dpkg --configure mutates"),
    ("dpkg --unpack pkg.deb", "dpkg --unpack writes"),
    ("apt install jq", "apt install writes"),
    ("apt remove jq", "apt remove"),
    ("apt update", "apt update rewrites package lists"),
    ("apt upgrade -y", "apt upgrade"),
    ("apt download jq", "apt download writes a .deb to cwd"),
    ("apt", "bare apt has no read subcommand"),
    ("apt-get install jq", "apt-get is not classified read-only at all"),
    ("apt-mark hold docker-ce", "apt-mark hold mutates selections"),
    ("apt-mark manual jq", "apt-mark manual mutates"),
    ("apt-mark unhold docker-ce", "apt-mark unhold mutates"),
    ("pipx install black", "pipx install"),
    ("pipx uninstall black", "pipx uninstall"),
    ("pipx run cowsay hi", "pipx run executes arbitrary code"),
    ("pipx upgrade-all", "pipx upgrade-all"),
    ("crontab", "bare crontab reads stdin and installs a crontab"),
    ("crontab myfile", "crontab FILE installs it"),
    ("crontab -r", "crontab -r deletes the crontab"),
    ("crontab -e", "crontab -e edits"),
    ("crontab -u ubuntu -r", "crontab -r for a user still deletes"),
    ("sensors -s", "sensors -s applies config to hardware"),
    ("sensors --set", "sensors --set writes"),
    ("ss -K", "ss -K closes sockets"),
    ("ss --kill", "ss --kill closes sockets"),
    ("ss -xKy", "ss -xKy — K hidden in a short cluster kills"),
    ("dmesg -C", "dmesg -C clears the ring buffer"),
    ("dmesg --clear", "dmesg --clear clears the ring buffer"),
    ("dmesg -c", "dmesg -c prints then clears"),
    ("dmesg --read-clear", "dmesg --read-clear prints then clears"),
    ("dmesg -xCy", "dmesg -xCy — C hidden in a short cluster clears"),
    (
        "nvidia-smi",
        "nvidia-smi is not admitted: no NVIDIA hardware in the fleet (#2052)",
    ),
    ("htop", "htop is interactive and never returns under the Bash tool"),
]

# MUST NOT auto-approve, and the verdict runs through the claude_guard tables.
REJECT_SSH = [
    ("ssh daniel-server", "no remote command -> interactive shell"),
    ("ssh daniel-server rm -rf /tmp/x", "remote rm deletes"),
    ("ssh daniel-server docker run alpine", "remote docker run executes"),
    ("ssh daniel-server systemctl restart traefik", "remote systemctl restart"),
    ("ssh daniel-server uv run ansible-playbook deploy.yml", "remote deploy writes"),
    ("ssh daniel-server 'cat a; rm b'", "bad stage inside the remote string"),
    ("ssh daniel-server docker ps | tee out.txt", "write stage in the local pipeline"),
    ("ssh unknown-host docker ps", "host outside SSH_HOSTS"),
    ("ssh root@unknown-host uptime", "user@ does not exempt the host check"),
    # forwarding / proxying / agent flags never reach the option whitelist
    ("ssh -o ProxyCommand=nc daniel-server uptime", "-o ProxyCommand execs locally"),
    ("ssh -o LocalCommand=id daniel-server uptime", "-o LocalCommand execs locally"),
    ("ssh -L 8080:localhost:80 daniel-server uptime", "-L opens a tunnel"),
    ("ssh -R 80:localhost:80 daniel-server uptime", "-R opens a reverse tunnel"),
    ("ssh -D 1080 daniel-server uptime", "-D opens a SOCKS proxy"),
    ("ssh -A daniel-server uptime", "-A forwards the agent"),
    ("ssh -F /tmp/cfg daniel-server uptime", "-F swaps the ssh config"),
    ("ssh -o BatchMode=yes", "options but no host"),
    ("ssh -i", "value flag with no value"),
    # secret reads over ssh land in the transcript
    ("ssh daniel-server cat /home/ubuntu/.ssh/id_ed25519", "remote private key"),
    ("ssh daniel-server grep -r x /home/ubuntu/.ssh", "remote .ssh directory"),
    ("ssh daniel-server cat /proc/self/environ", "remote process environment"),
    ("ssh daniel-pi cat /home/ubuntu/.aws/credentials", "remote aws credentials"),
    ("ssh daniel-server cat /home/ubuntu/server/.env", "remote dotenv"),
    # a glob is expanded by the REMOTE shell, after these checks run
    ("ssh daniel-server cat /proc/self/enviro?", "glob can become a secret path"),
    ("ssh daniel-server ssh daniel-pi uptime", "second hop"),
]

APPROVE = APPROVE_LOCAL + APPROVE_SSH
REJECT = REJECT_LOCAL + REJECT_SSH


def _failures_approve(table=None):
    return [
        (c, l) for c, l in (APPROVE if table is None else table) if classify(c) is None
    ]


def _failures_reject(table=None):
    return [
        (c, l)
        for c, l in (REJECT if table is None else table)
        if classify(c) is not None
    ]


def _approve_report(bad):
    return "Expected APPROVE but got a prompt:\n" + "\n".join(
        f"  [{l}] {c!r}" for c, l in bad
    )


def _reject_report(bad):
    return "Expected REJECT but got auto-approve:\n" + "\n".join(
        f"  [{l}] {c!r} -> {classify(c)!r}" for c, l in bad
    )


def test_approves_read_only_commands():
    assert not (bad := _failures_approve(APPROVE_LOCAL)), _approve_report(bad)


def test_rejects_unsafe_commands():
    assert not (bad := _failures_reject(REJECT_LOCAL)), _reject_report(bad)


@needs_deployed_tables
def test_approves_read_only_commands_over_ssh():
    assert not (bad := _failures_approve(APPROVE_SSH)), _approve_report(bad)


@needs_deployed_tables
def test_rejects_unsafe_commands_over_ssh():
    assert not (bad := _failures_reject(REJECT_SSH)), _reject_report(bad)


def misplaced_vectors(local, ssh):
    """Vectors on the wrong side of the split: ssh tokens in `local`, none in `ssh`."""
    return [c for c, _ in local if _HAS_SSH.search(c)] + [
        c for c, _ in ssh if not _HAS_SSH.search(c)
    ]


def test_every_ssh_vector_is_in_an_ssh_list():
    """The split is by hand; this is what keeps a table-dependent vector out of `_LOCAL`.

    A vector with an `ssh` token that sits in a `_LOCAL` list runs unconditionally, so under
    the stand-in it passes on the copy -- the hole the `_SSH` skip exists to show. The floors
    are the counts at the split (10 approve, 25 reject); an `_SSH` list that emptied out
    would otherwise skip nothing and prove nothing.
    """
    bad = misplaced_vectors(APPROVE_LOCAL + REJECT_LOCAL, APPROVE_SSH + REJECT_SSH)
    assert not bad, "vectors on the wrong side of the split:\n" + "\n".join(
        f"  {c!r}" for c in bad
    )
    assert len(APPROVE_SSH) >= 10
    assert len(REJECT_SSH) >= 25


def test_placement_check_is_clean_on_a_correct_split():
    assert (
        misplaced_vectors([("ls", ""), ("cat a | head", "")], [("ssh h ls", "")]) == []
    )


def test_placement_check_is_flagged_on_a_misplaced_vector():
    planted = "cat a | ssh daniel-server ls"
    assert misplaced_vectors([("ls", ""), (planted, "")], []) == [planted]
    assert misplaced_vectors([], [("ls", "")]) == ["ls"]


def test_ssh_tests_skip_under_the_stand_in_and_run_against_the_deploy():
    """The skip tracks what fed the tables, not whether a directory exists.

    `conftest.py` marks its fake package; the real one carries no such attribute. A HOME
    pointed at an empty directory changes neither, which is why the marker is the signal.
    """
    guard = sys.modules["claude_guard"]
    if _STAND_IN:
        assert guard.__claude_guard_stand_in__ is True
        assert not hasattr(guard, "__file__")
    else:
        assert not hasattr(guard, "__claude_guard_stand_in__")
        assert guard.__file__


if __name__ == "__main__":
    import sys

    tables = [
        ("APPROVE_LOCAL", APPROVE_LOCAL, _failures_approve),
        ("APPROVE_SSH", APPROVE_SSH, _failures_approve),
        ("REJECT_LOCAL", REJECT_LOCAL, _failures_reject),
        ("REJECT_SSH", REJECT_SSH, _failures_reject),
    ]
    total_bad = 0
    for name, table, failures in tables:
        bad = failures(table)
        total_bad += len(bad)
        note = (
            " (against conftest's stand-in tables)"
            if _STAND_IN and "SSH" in name
            else ""
        )
        print(f"{name}: {len(table) - len(bad)}/{len(table)} passed{note}")
        for c, l in bad:
            print(f"  FAIL [{l}]: {c!r} -> {classify(c)!r}")
    print(f"\n{'ALL PASS' if total_bad == 0 else str(total_bad) + ' FAILURES'}")
    sys.exit(1 if total_bad else 0)
