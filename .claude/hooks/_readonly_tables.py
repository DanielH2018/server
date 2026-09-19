#!/usr/bin/env python3
# gen-hooks: library
#   reason: imported by auto-approve-readonly.py
"""Allow-list tables for the auto-approve-readonly Bash classifier.

Data only: the program names that are read-only under any arguments, and the ssh flags,
options and secret-path patterns that bound which remote commands the classifier will
reconstruct and re-classify. Nothing here executes a decision — `auto-approve-readonly.py`
holds every guard and applies these, including the two patterns whose match is itself a
refusal (`_SSH_SECRET`, `_SSH_GLOB`).

That module imports them by bare name, which resolves both ways it is loaded: the hooks dir
is ``sys.path[0]`` when Claude Code runs the hook, and the tests insert that dir before
loading the hook by path. Same convention as ``_hook_common.py``. Stdlib-only except for the
`claude_guard` import below, which `_claude_guard` bootstraps onto `sys.path`.
"""

import re
import sys

# The same fail-open idiom the .sh shims use for a failed cd (test_hook_shim_fail_open.py):
# one line on stderr, exit 0, nothing on stdout, so the prompt stands. Without this the
# ImportError propagates as exit 1 with a two-part traceback — the harness treats any
# nonzero exit other than 2 as non-blocking, so that is fail-open too, but it reads as a
# crash rather than a classifier declining to run. Raising stays the library behaviour of
# `_claude_guard` itself (conftest.py and test_claude_guard_import.py rely on it); the exit
# belongs here, on the module every allow-side entry point imports.
try:
    import _claude_guard  # noqa: F401  (bootstraps claude_guard onto sys.path)
except ImportError as exc:
    print(
        f"_readonly_tables: classifier did not run ({exc}) — command falls through to the "
        "normal permission flow",
        file=sys.stderr,
    )
    sys.exit(0)
from claude_guard.tables import REMOTE_READONLY_VERBS, SECRET_PATH_RE, TRUSTED_SSH_HOSTS


# Programs that cannot write or exec under ANY arguments.
#
# Derived from the dotfiles package's `REMOTE_READONLY_VERBS`, not copied: until 2026-09-18
# this was a 92-name literal sharing 89 names with the package's 96, converged by hand once
# (server #1979, dotfiles #520) with nothing to catch the two drifting apart again (#2052).
# Now the shared names have one home and this file states only the delta, each name with
# its reason. `boundary_violations()` in tests/test_claude_guard_import.py still checks the
# guarded-vs-bare axis: a verb the package guards that lands here bare, or the reverse, goes
# red under `prek run` on a deployed host.
#
# Deliberately excludes commands with a write/exec mode: env (`env CMD`), less/more (`!cmd`
# escape), command/xargs/timeout/nice/... (exec wrappers), sed/awk (-i, system()),
# tee/dd/xxd/mount/stty (write), sort/uniq/find/ip/... (guarded in auto-approve-readonly.py).
#
# DECIDED: derive a LOCAL table from a REMOTE one. A name the package adds to
# `REMOTE_READONLY_VERBS` widens local auto-approve on the next `chezmoi apply` with no edit
# here — the coupling a package-exported `READONLY_BASE` would avoid, and the dotfiles
# follow-up #2052 files. Accepted because the alternative was the hand-synced copy, whose
# drift no test could see; the boundary test above sees a guarded name arrive, and the CI
# stand-in's diff test (`test_the_ci_stand_in_matches_the_deployed_tables`) sees any change
# to the set at all.

# Package names the server admits only through a `HANDLERS` guard in auto-approve-readonly.py
# (the package guards them too, in checks/remote.py). TIER1's contract is "read-only under
# ANY argument", which none of these meets: journalctl `--vacuum-*`/`--rotate`, dmesg
# `-C`/`--clear`, ss `-K`/`--kill`, rg `--pre`/`--hostname-bin`, sensors `-s`/`--set`.
_GUARDED_LOCALLY = frozenset({"journalctl", "dmesg", "ss", "rg", "sensors"})

# Package names the server admits nowhere, bare or guarded.
_NOT_ADMITTED = frozenset(
    {
        # Interactive: it never returns under the Bash tool, so it only ever times out.
        "htop",
        # No NVIDIA hardware in the fleet (daniel-server is Intel XE; every `nvidia` in the
        # tree reads "on a future AMD/NVIDIA host"). A guard for a binary no host has is dead
        # code; port the package's `_nvidia_smi_readonly` when a host gains one (#2052).
        "nvidia-smi",
    }
)

# Names read-only locally that the package keeps out of its remote table: `cd` and `false`
# are meaningless over ssh, and `printenv` prints every exported variable of the REMOTE
# shell — locally the transcript already runs under this environment.
_LOCAL_ONLY = frozenset({"cd", "false", "printenv"})

TIER1 = (
    frozenset(REMOTE_READONLY_VERBS) - _GUARDED_LOCALLY - _NOT_ADMITTED
) | _LOCAL_ONLY


# Homelab hosts whose read-only commands may auto-approve. Anything else falls
# through to a prompt: reaching an unknown host is itself worth confirming.
# Defined once, in the dotfiles `claude_guard` package.
SSH_HOSTS = frozenset(TRUSTED_SSH_HOSTS)

# ssh flags that change only how we connect, never what runs. Everything absent
# is refused, which is what keeps -L/-R/-D (forwarding), -F (alternate config),
# -A (agent forwarding) and -J/-W (proxying) out.
_SSH_FLAGS = {"-q", "-T", "-n", "-4", "-6"}
_SSH_VALUE_FLAGS = {"-i", "-p", "-l", "-o"}

# -o takes arbitrary config, including ProxyCommand/LocalCommand — which execute
# a command on THIS machine. Whitelisting the key is what makes -o safe.
_SSH_OPTIONS = {
    "batchmode",
    "connectionattempts",
    "connecttimeout",
    "identitiesonly",
    "loglevel",
    "serveralivecountmax",
    "serveraliveinterval",
    "stricthostkeychecking",
}

# Reading a secret over ssh dumps it into the transcript, so the remote side is
# held to a stricter standard than the local one (local `cat ~/.ssh/id_ed25519`
# is already TIER1-approved). Mirrors the SECRET_RE in the user-level
# allow-readonly-remote.sh, which governs the same traffic.
# Defined once, in the dotfiles `claude_guard` package.
_SSH_SECRET = SECRET_PATH_RE

# A glob is expanded by the REMOTE shell, after our checks have run, so a literal
# that _SSH_SECRET doesn't match (`/proc/self/enviro?`) can still become a secret
# path over there. We can't see the remote filesystem, so we refuse the pattern.
_SSH_GLOB = re.compile(r"[*?\[\]\\]")

# Verbs that read except under one flag, for auto-approve-readonly.py's `_flag_guarded`:
# the long options, and the letters that mean the same inside a short cluster (`-xKy`).
_FLAG_MUTATES = {
    "ss": (("--kill",), "K"),  # -K/--kill closes sockets (#1898)
    "sensors": (("--set",), "s"),  # -s/--set writes config back to the hardware
    # -C/--clear clears the ring buffer, -c/--read-clear prints then clears. Mirrors the
    # package's `_DMESG_MUTATE` exactly (#2052); `-n`/`-D`/`-E` set the console log level
    # too, but need CAP_SYSLOG, which the `sudo` deny withholds (#2078 for the replay).
    "dmesg": (("--clear", "--read-clear"), "Cc"),
}

# journalctl flags that delete, rotate or reconfigure the journal; anything else reads.
_JOURNAL_WRITE = (
    "--rotate",
    "--vacuum-size",
    "--vacuum-time",
    "--vacuum-files",
    "--flush",
    "--sync",
    "--relinquish-var",
    "--smart-relinquish-var",
    "--update-catalog",
    "--setup-keys",
)
