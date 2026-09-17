#!/bin/bash
# sshd forced command for the pi-peer-backup key on daniel-pi (issue #1927).
#
# The key exists for exactly one operation: the nightly CronJob's `rsync --server --sender`
# of the wg-easy config directory, run through `sudo` because the files are root-owned. Before
# this wrapper the key was authorized with no options, so any principal that could read the
# `pi-peer-backup-ssh` Secret held an interactive shell as a NOPASSWD-sudo user on the Pi.
# `restrict,command="<this> <dir>"` in authorized_keys (tasks/main.yml) makes sshd run this
# script instead of whatever the client asked for, with the client's request in
# SSH_ORIGINAL_COMMAND.
#
# The request is validated STRUCTURALLY rather than against a pinned argv: the short-option
# blob (`-logDtpre.iLsfxCIvu` on 2026-09-17) is negotiated between the two rsync versions and
# moves when the alpine base image bumps, so a literal `command=` would break the nightly as a
# Kuma DOWN 2.5 days after the next Renovate merge. What is fixed: the verb must be a sender
# (a receiver would write to the Pi), the only long option allowed is `--timeout=N`
# (`--remove-source-files`, `--files-from`, `--log-file` and friends are long options), and the
# source directory is FORCED from this script's argument rather than taken from the client, so
# a request naming another path serves the wg-easy directory or nothing.
#
# The exec'd argv stays `sudo rsync ...`, so it works whether the Pi's sudoers rule is
# `NOPASSWD: ALL` or scoped to /usr/bin/rsync.
set -u

SRC="${1:?usage: pi-peer-backup-shell <source directory>}"
CMD="${SSH_ORIGINAL_COMMAND:-}"

reject() {
  echo "pi-peer-backup-shell: rejected: $1" >&2
  logger -t pi-peer-backup-shell "rejected: $1 (${CMD:0:200})" 2>/dev/null || true
  exit 1
}

read -ra WORDS <<<"$CMD"
n=${#WORDS[@]}
# `sudo rsync --server --sender <opts...> . <dir>` — at least the four verb words plus `.` and
# the directory. An empty command (an interactive login) fails this first.
[[ $n -ge 6 ]] || reject "not an rsync request"
[[ ${WORDS[0]} == sudo && ${WORDS[1]} == rsync ]] || reject "not sudo rsync"
[[ ${WORDS[2]} == --server && ${WORDS[3]} == --sender ]] || reject "not a sender"
[[ ${WORDS[n - 2]} == . && ${WORDS[n - 1]} == "$SRC" ]] || reject "path is not $SRC"
for opt in "${WORDS[@]:4:n-6}"; do
  [[ $opt =~ ^-[A-Za-z.]+$ || $opt =~ ^--timeout=[0-9]+$ ]] || reject "option $opt"
done

exec sudo rsync --server --sender "${WORDS[@]:4:n-6}" . "$SRC"
