#!/usr/bin/env python3
"""Table-driven tests for the auto-approve-readonly Bash classifier.

The classifier is a security boundary: it may only ever REDUCE permission
prompts for *provably* read-only commands, and must NEVER auto-approve a
command that can write, delete, or execute. These tables lock that contract.

Run: uv run pytest .claude/hooks
(Still importable standalone — it loads the hook by path, no third-party deps.)
"""

import importlib.util
import os

_HOOK = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "auto-approve-readonly.py"
)
_spec = importlib.util.spec_from_file_location("auto_approve_readonly", _HOOK)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
classify = _mod.classify
classify_remote = _mod.classify_remote


# MUST auto-approve: provably read-only.
APPROVE = [
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
]

# MUST NOT auto-approve: can write, delete, or execute (or unparseable).
REJECT = [
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


def _failures_approve():
    return [(c, l) for c, l in APPROVE if classify(c) is None]


def _failures_reject():
    return [(c, l) for c, l in REJECT if classify(c) is not None]


def test_approves_read_only_commands():
    bad = _failures_approve()
    assert not bad, "Expected APPROVE but got a prompt:\n" + "\n".join(
        f"  [{l}] {c!r}" for c, l in bad
    )


def test_rejects_unsafe_commands():
    bad = _failures_reject()
    assert not bad, "Expected REJECT but got auto-approve:\n" + "\n".join(
        f"  [{l}] {c!r} -> {classify(c)!r}" for c, l in bad
    )


# classify_remote answers `ask` rules, so it must speak only for the traffic that
# needs it. A read-only command with no ssh in it is already handled at PreToolUse.

REMOTE_ONLY = [
    ("ssh daniel-server docker ps", "plain remote command"),
    ("ssh daniel-server docker ps | head -3", "ssh as one stage of a pipeline"),
]

LOCAL_NOT_REMOTE = [
    ("ls -la", "read-only, but purely local"),
    ("cat a.txt | grep foo", "read-only local pipeline"),
    ("git status", "read-only local git"),
]


def test_permission_request_covers_remote_commands():
    bad = [(c, l) for c, l in REMOTE_ONLY if classify_remote(c) is None]
    assert not bad, "Expected a PermissionRequest allow:\n" + "\n".join(
        f"  [{l}] {c!r}" for c, l in bad
    )


def test_permission_request_stays_out_of_local_commands():
    bad = [(c, l) for c, l in LOCAL_NOT_REMOTE if classify_remote(c) is not None]
    assert not bad, "PermissionRequest spoke for a non-ssh command:\n" + "\n".join(
        f"  [{l}] {c!r} -> {classify_remote(c)!r}" for c, l in bad
    )


def test_permission_request_never_widens_classify():
    # Everything classify() refuses must stay refused here -- this entry point may
    # only ever narrow it.
    bad = [(c, l) for c, l in REJECT if classify_remote(c) is not None]
    assert not bad, "PermissionRequest approved a rejected command:\n" + "\n".join(
        f"  [{l}] {c!r} -> {classify_remote(c)!r}" for c, l in bad
    )


if __name__ == "__main__":
    import sys

    fa, fr = _failures_approve(), _failures_reject()
    print(f"APPROVE cases: {len(APPROVE) - len(fa)}/{len(APPROVE)} passed")
    for c, l in fa:
        print(f"  MISS approve [{l}]: {c!r}")
    print(f"REJECT cases:  {len(REJECT) - len(fr)}/{len(REJECT)} passed")
    for c, l in fr:
        print(f"  !! FALSE-APPROVE [{l}]: {c!r} -> {classify(c)!r}")
    total_bad = len(fa) + len(fr)
    print(f"\n{'ALL PASS' if total_bad == 0 else str(total_bad) + ' FAILURES'}")
    sys.exit(1 if total_bad else 0)
