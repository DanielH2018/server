#!/bin/sh
# /usr/local/bin/rootq: read root-only host state through homelab-rootq's one sudoers line
# (issue #6323). Installed by tasks/rootq.yml.
#
# Usage: rootq nft | ufw | sshd | crontab
#        rootq ls PATH | stat PATH | cat PATH
#
# The root script refuses any argument it does not accept, so nothing is validated here. sudo
# is named by path and stdin is /dev/null, so `sudo -n` never reads a password.

case "${1-}" in
  -h | --help)
    sed -n '2,/^$/s/^# \{0,1\}//p' "$0"
    exit 0
    ;;
esac
exec /usr/bin/sudo -n /usr/local/sbin/homelab-rootq "$@" < /dev/null
