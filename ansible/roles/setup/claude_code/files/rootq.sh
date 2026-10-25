#!/bin/sh
# /usr/local/bin/rootq: read root-only host state through homelab-rootq's one sudoers line
# (issue #6323). Installed by tasks/rootq.yml.
#
# Usage: rootq stat PATH | sha256 PATH | matches PATH REPO_FILE   (tier 1, no content)
#        rootq nft | ufw | sshd | crontab | cat PATH              (tier 2, redacted)
#
# The root script refuses any argument it does not accept, so nothing else is validated here.
# `matches` is the exception: this shim hashes REPO_FILE as the caller and passes only the
# hash, so root never opens a caller-named file. REPO_FILE must be relative, hold no `..`
# and resolve inside the checkout around the working directory. sudo is named by path and
# stdin is /dev/null, so `sudo -n` never reads a password. HOMELAB_ROOTQ_SUDO is a test seam:
# this shim runs as the caller, so naming another program gains nothing.

sudo=${HOMELAB_ROOTQ_SUDO:-/usr/bin/sudo}
script=/usr/local/sbin/homelab-rootq

refuse() {
  echo "refused: $*" >&2
  exit 2
}

case "${1-}" in
  -h | --help)
    sed -n '2,/^$/s/^# \{0,1\}//p' "$0"
    exit 0
    ;;
  matches)
    [ "$#" -eq 3 ] || refuse "matches takes PATH REPO_FILE"
    case "$3" in
      /* | .. | ../* | */.. | */../*) refuse "REPO_FILE must be relative and hold no .." ;;
    esac
    top=$(git rev-parse --show-toplevel 2> /dev/null) || refuse "run matches inside a checkout"
    file=$(realpath -e -- "$3" 2> /dev/null) || refuse "$3 does not exist"
    case "$file" in
      "$top"/*) ;;
      *) refuse "$3 resolves outside the checkout" ;;
    esac
    [ -f "$file" ] || refuse "$3 is not a regular file"
    hash=$(sha256sum < "$file") || refuse "cannot hash $3"
    exec "$sudo" -n "$script" matches "$2" "${hash%% *}" < /dev/null
    ;;
esac
exec "$sudo" -n "$script" "$@" < /dev/null
