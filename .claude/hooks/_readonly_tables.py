#!/usr/bin/env python3
"""Allow-list tables for the auto-approve-readonly Bash classifier.

Data only: the program names that are read-only under any arguments, and the ssh flags,
options and secret-path patterns that bound which remote commands the classifier will
reconstruct and re-classify. Nothing here decides — `auto-approve-readonly.py` holds every
guard, and reads these.

That module imports them by bare name, which resolves both ways it is loaded: the hooks dir
is ``sys.path[0]`` when Claude Code runs the hook, and the tests insert that dir before
loading the hook by path. Same convention as ``_hook_common.py``. Stdlib-only.
"""

import re


# Programs that cannot write or exec under ANY arguments
# Deliberately excludes commands with a write/exec mode: env (`env CMD`),
# less/more (`!cmd` escape), command/xargs/timeout/nice/... (exec wrappers),
# sed/awk (-i, system()), tee/dd/xxd/mount/stty (write), sort/uniq/find/ip/...
# (guarded below instead).
TIER1 = {
    # text / file readers and stdout-only filters (no output-file option)
    "ls",
    "cat",
    "head",
    "tail",
    "wc",
    "nl",
    "tac",
    "rev",
    "fold",
    "cut",
    "tr",
    "column",
    "comm",
    "grep",
    "egrep",
    "fgrep",
    "zgrep",
    "zcat",
    "od",
    "hexdump",
    "strings",
    "stat",
    "file",
    "readlink",
    "realpath",
    "basename",
    "dirname",
    "tree",
    "cksum",
    "md5sum",
    "sha1sum",
    "sha256sum",
    "sha512sum",
    "b2sum",
    "jq",
    # system / inspection
    "pwd",
    "whoami",
    "id",
    "groups",
    "hostname",
    "uname",
    "arch",
    "uptime",
    "date",
    "w",
    "who",
    "last",
    "lastlog",
    "df",
    "du",
    "free",
    "ps",
    "top",
    "vmstat",
    "iostat",
    "mpstat",
    "sar",
    "nproc",
    "lscpu",
    "lsblk",
    "lsusb",
    "lspci",
    "lsmod",
    "lsattr",
    "findmnt",
    "blkid",
    "getconf",
    "getent",
    "locale",
    "printenv",
    "lsof",
    "ss",
    "netstat",
    "dig",
    "host",
    "nslookup",
    "apt-cache",
    "echo",
    "printf",
    "seq",
    "true",
    "false",
    "which",
    "type",
    "cd",
    # package / host queries with no write mode under any argument
    "lsb_release",
    "mailq",
    "dpkg-query",
}


# Homelab hosts whose read-only commands may auto-approve. Anything else falls
# through to a prompt: reaching an unknown host is itself worth confirming.
SSH_HOSTS = {"daniel-server", "daniel-pi"}

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
_SSH_SECRET = re.compile(
    r"\.env|\.ssh(/|\s|$)|id_rsa|id_ed25519|id_ecdsa|\.aws/credentials|\.aws/config"
    r"|\.gnupg(/|\s|$)|\.netrc|\.pypirc|\.npmrc|/secrets(/|\s|$)|\.git-credentials"
    r"|\.kube/config|\.docker/config\.json|\.config/gh/hosts\.yml|\.config/gcloud/"
    r"|\.config/rclone/rclone\.conf|terraform\.tfstate|\.bash_history|\.claude\.json"
    r"|/etc/shadow|/etc/gshadow|/proc/\S*environ|\.pem($|[^a-z])|\.key($|[^a-z])"
    r"|\.p12($|[^a-z])|\.pfx($|[^a-z])",
    re.IGNORECASE,
)

# A glob is expanded by the REMOTE shell, after our checks have run, so a literal
# that _SSH_SECRET doesn't match (`/proc/self/enviro?`) can still become a secret
# path over there. We can't see the remote filesystem, so we refuse the pattern.
_SSH_GLOB = re.compile(r"[*?\[\]\\]")
