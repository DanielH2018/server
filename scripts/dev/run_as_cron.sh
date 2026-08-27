#!/usr/bin/env bash
#
# run_as_cron.sh — run a command in the environment cron actually gives it.
#
# Every unattended job here inherits a far smaller environment than the shell it was
# written in, and the resulting failures do not crash. They return empty and exit 0,
# which is strictly worse: nothing alerts, and the output looks plausible. Two are
# documented on this host (both found on 2026-08-07, the second only after the first
# was "fixed" — so finding one is evidence the other is live):
#
#   1. PATH. Cron runs with PATH=/usr/bin:/bin. /usr/local/bin is NOT on it, and that
#      is where kubectl lives (a symlink to k3s). ssh, docker and coreutils are in
#      /usr/bin and keep working, so only the k8s half of a job goes dark and
#      shutil.which("kubectl") quietly returns None.
#   2. KUBECONFIG. Unset, k3s's kubectl falls back to /etc/rancher/k3s/k3s.yaml, which
#      is root-owned 0640. An interactive shell exports KUBECONFIG=~/.kube/config, so
#      this fails only when nobody is watching — a stderr warning plus an empty item
#      list, indistinguishable from a cluster with no workloads.
#
# A third trap rides along: cron runs /bin/sh, not the interactive zsh these scripts are
# written and tested in. zsh does not word-split unquoted expansions and sh does, so a
# gate that works by hand can crash under cron naming a file that exists.
#
# Interactive testing cannot reproduce any of the three. This wrapper is the reproduction,
# so a cron-invoked script can be exercised under its real environment before it ships.
#
# Usage:
#   scripts/dev/run_as_cron.sh [--expect-output] -- <command> [args...]
#
#   --expect-output   Fail if the command exits 0 having written nothing to stdout.
#                     This is the signature of the whole class: a job that reports
#                     "nothing there" while exiting clean. Pass it for any command whose
#                     job is to FIND something (list workloads, count volumes, resolve a
#                     tool); omit it for a command whose success is genuinely silent.
#
# Exit codes:
#   the command's own exit code, or 66 for an --expect-output violation (a silent
#   empty success), chosen to be distinguishable from anything the command returns.
#
set -euo pipefail

readonly EMPTY_SUCCESS_EXIT=66

usage() {
    sed -n '3,32p' "$0" | sed 's|^# \{0,1\}||'
    exit "${1:-0}"
}

expect_output=0
while [ $# -gt 0 ]; do
    case "$1" in
        --expect-output) expect_output=1; shift ;;
        -h|--help)       usage 0 ;;
        --)              shift; break ;;
        -*)              echo "run_as_cron.sh: unknown option '$1'" >&2; usage 2 ;;
        *)               break ;;
    esac
done

if [ $# -eq 0 ]; then
    echo "run_as_cron.sh: no command given" >&2
    usage 2
fi

# The environment cron actually supplies. `env -i` clears everything first, so anything
# not named here is genuinely absent — KUBECONFIG above all, which is the whole point.
# HOME and LOGNAME are set because cron does set them; PATH is cron's, not the shell's.
#
# DECIDED: /bin/sh, not bash — cron uses sh, and reproducing only the PATH half while
# keeping a bash-or-zsh interpreter leaves the word-splitting trap unreproduced.
stdout_file="$(mktemp)"
trap 'rm -f "$stdout_file"' EXIT

# DECIDED: the command runs through `sh -c`, not `exec "$@"`. A crontab line IS a shell
# line — cron hands the whole thing to /bin/sh — so exec'ing argv directly would refuse
# every builtin, pipeline and redirect a real cron job is allowed to use, and would fail
# them with a 127 that reads exactly like the PATH trap this script exists to expose.
# One argument is taken as that shell line verbatim; several are quoted into one.
if [ $# -eq 1 ]; then
    cmdline="$1"
else
    cmdline="$(printf '%q ' "$@")"
fi

# DECIDED: a plain redirect into a file, then cat — NOT `tee` via process substitution.
# The subshell tee runs in is reaped asynchronously, so the -s emptiness test below can
# read the file before tee has flushed and call a talkative command silent. stderr stays
# on stderr so a warning is not mistaken for output.
set +e
env -i \
    HOME="${HOME:-/home/ubuntu}" \
    LOGNAME="${LOGNAME:-$(id -un)}" \
    USER="${USER:-$(id -un)}" \
    SHELL=/bin/sh \
    PATH=/usr/bin:/bin \
    /bin/sh -c "$cmdline" \
    > "$stdout_file"
rc=$?
set -e
cat "$stdout_file"

if [ "$rc" -ne 0 ]; then
    echo "run_as_cron.sh: command exited $rc under the cron environment" >&2
    exit "$rc"
fi

if [ "$expect_output" -eq 1 ] && [ ! -s "$stdout_file" ]; then
    cat >&2 <<'MSG'
run_as_cron.sh: the command exited 0 and wrote nothing to stdout.

Under --expect-output that is a FAILURE, not a pass. It is the exact signature of the
cron-environment class: the tool was unfindable or the config unreadable, the job
reported "nothing there", and it exited clean. Resolve the tool and the kubeconfig in
the script rather than trusting the environment — check the kubeconfig is READABLE, not
merely present, pass it as an explicit --kubeconfig, and treat a missing tool or config
as a fault that exits non-zero without overwriting prior good output.

scripts/infra_map/gen_infra_map.py's find_tool() / find_kubeconfig() / MissingToolError
are the worked example in this repo.
MSG
    exit "$EMPTY_SUCCESS_EXIT"
fi
