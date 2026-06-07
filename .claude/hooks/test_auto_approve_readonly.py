#!/usr/bin/env python3
"""Table-driven tests for the auto-approve-readonly Bash classifier.

The classifier is a security boundary: it may only ever REDUCE permission
prompts for *provably* read-only commands, and must NEVER auto-approve a
command that can write, delete, or execute. These tables lock that contract.

Runnable two ways:
  * standalone (no deps):   python3 .claude/hooks/test_auto_approve_readonly.py
  * under pytest:           python3 -m pytest .claude/hooks/test_auto_approve_readonly.py
"""
import importlib.util
import os

_HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto-approve-readonly.py")
_spec = importlib.util.spec_from_file_location("auto_approve_readonly", _HOOK)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
classify = _mod.classify


# --- (command, label) tables ------------------------------------------------

# MUST auto-approve: provably read-only.
APPROVE = [
    # --- existing behavior (regression) ---
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

    # --- NEW: cd is read-only ---
    ("cd /home/ubuntu/server", "cd changes cwd only"),
    ("cd /srv && ls", "cd then ls"),

    # --- NEW: sequential operators ; && || ---
    ("cat a; cat b", "two reads joined by ;"),
    ("echo hi; ls; pwd", "three reads joined by ;"),
    ("ls && cat foo", "&& sequence"),
    ("false || ls", "|| sequence"),

    # --- NEW: newline-separated statements ---
    ("cat a\ncat b", "two reads on separate lines"),
    ("echo '=== a ==='\ncat a\necho '=== b ==='\ncat b", "header/cat blocks"),

    # --- NEW: write-free redirects ---
    ("cat .yamllint 2>/dev/null", "stderr to /dev/null"),
    ("ls >/dev/null", "stdout to /dev/null"),
    ("docker ps 2>&1", "fd duplication 2>&1"),
    ("grep -r foo . 2>/dev/null | head", "redirect inside pipeline"),

    # --- NEW: awk read-only programs ---
    ("awk '{ print length, FILENAME }' file", "awk print length"),
    ("awk -F: '{print $1}' /etc/passwd", "awk with -F field sep"),
    ("ls | awk '{print $9}'", "awk in a pipeline"),
    ("awk 'NR==1' file", "awk line selection"),

    # --- NEW: sed read-only scripts ---
    ("sed -n '1,5p' file", "sed print range"),
    ("sed 's/foo/bar/' file", "sed substitution to stdout"),
    ("echo x | sed 's/x/y/'", "sed in a pipeline"),
    (r"sed -E 's/(public key:|private key:).*/\1 [redacted]/'", "the wireguard redaction sed"),

    # --- adversarial edge cases that are genuinely read-only ---
    ("grep foo file >/dev/null 2>&1", "combined >/dev/null 2>&1"),
    ('echo "a; rm b"', "operators inside quotes are data, not syntax"),
    ('echo "x && y | z"', "quoted pipe/and is data"),

    # --- NEW: the original motivating command ---
    ("cd /home/ubuntu/server\n"
     'echo "=== .ansible-lint ==="; cat .ansible-lint\n'
     'echo ""; cat .yamllint 2>/dev/null\n'
     "awk '{ print length, FILENAME }' ansible/roles/containers/x/tasks/main.yml",
     "full multi-line exploration command"),
]

# MUST NOT auto-approve: can write, delete, or execute (or unparseable).
REJECT = [
    # --- existing behavior (regression) ---
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

    # --- NEW-feature dangerous forms must still reject ---
    ("ls > out.txt", "redirect writes a real file"),
    ("cat a >> log.txt", "append writes a real file"),
    ("ls &", "backgrounding"),
    ("(cat a)", "subshell"),
    ("cat a; rm b", "one bad stage in a ; sequence"),
    ("ls && rm -rf x", "bad stage after &&"),
    ("cat a | tee out", "tee write inside pipeline"),
    ("cat a && echo $(rm x)", "substitution hidden after &&"),
    ("cat a\nrm b", "bad stage on a second line"),

    # --- awk dangerous forms ---
    ('awk \'BEGIN{system("rm -rf x")}\'', "awk system() executes"),
    ('awk \'{print > "out.txt"}\' file', "awk redirects to a file"),
    ('awk \'{print | "sh"}\' file', "awk pipes to a command"),
    ('awk \'BEGIN{while(("ls"|getline l)>0) print l}\'', "awk getline from command"),
    ("awk -f prog.awk file", "awk -f program file (uninspectable)"),
    ("gawk -i inplace '{print}' file", "gawk -i inplace edits files"),

    # --- adversarial false-approve guards (the dangerous direction) ---
    ("awk '{print}' > out.txt", "shell redirect to file after a safe awk"),
    ("diff <(ls) <(ls)", "process substitution"),
    ("cat a|rm b", "no-space pipe into a mutator"),
    (">/dev/null", "redirect with no command"),
    ("sed '/foo/w out' file", "sed w command reached via address"),
    ("cat a > b 2>/dev/null", "real-file write alongside a safe redirect"),

    # --- sed dangerous forms ---
    ("sed -i 's/a/b/' file", "sed -i edits in place"),
    ("sed 's/a/b/w out.txt' file", "sed s///w writes a file"),
    ("sed 's/a/b/e' file", "sed s///e executes"),
    ("sed -n 'w out.txt' file", "sed w command writes"),
    ("sed '1e cat /etc/shadow' file", "sed e command executes"),
    ("sed -f script.sed file", "sed -f program file (uninspectable)"),
]


def _failures_approve():
    return [(c, l) for c, l in APPROVE if classify(c) is None]


def _failures_reject():
    return [(c, l) for c, l in REJECT if classify(c) is not None]


def test_approves_read_only_commands():
    bad = _failures_approve()
    assert not bad, "Expected APPROVE but got a prompt:\n" + "\n".join(
        f"  [{l}] {c!r}" for c, l in bad)


def test_rejects_unsafe_commands():
    bad = _failures_reject()
    assert not bad, "Expected REJECT but got auto-approve:\n" + "\n".join(
        f"  [{l}] {c!r} -> {classify(c)!r}" for c, l in bad)


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
