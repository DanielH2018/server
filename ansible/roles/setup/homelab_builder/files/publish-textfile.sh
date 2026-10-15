#!/bin/sh
# Copy one unit's metric file into the node-exporter textfile directory.
#
# Usage: publish-textfile <prefix> <file>
#   e.g. publish-textfile homelab_builder_ /var/lib/homelab-builder/homelab_builder.prom
#
# The builder and signer units run this through an `ExecStopPost=-+` line: `+` runs it as root,
# outside the unit's sandbox, because neither service user can write the textfile directory, and
# `-` keeps a failed copy from failing the unit. It runs as root over a file the service user
# wrote, so it copies only the series named with the unit's prefix, and their HELP and TYPE
# lines. A file that is a symlink, or not a regular file, is refused. It is plain sh under
# /bin/sh, never an interpreter under a service user's state directory, which that user could
# rewrite.
#
# The write is M3's rule: mktemp in the directory, chmod 0644, then mv, because a bare mktemp file
# is 0600 and the exporter's user cannot read it. A missing directory exits 1; this never creates
# it, so a host whose exporter is not set up fails loudly (h3-pi-pilot.md, *Shared names*,
# Metrics).
#
# TEXTFILE_DIR overrides the directory for the tests; the units never set it.
set -eu

prefix=${1:?usage: publish-textfile <prefix> <file>}
src=${2:?usage: publish-textfile <prefix> <file>}
dir=${TEXTFILE_DIR:-/var/lib/node-exporter-textfile}

case $prefix in
  *[!a-z_]* | '') echo "publish-textfile: prefix $prefix is not [a-z_]+" >&2; exit 64 ;;
esac
if [ ! -d "$dir" ]; then
  echo "publish-textfile: $dir is missing; the exporter is not set up" >&2
  exit 1
fi
if [ -L "$src" ] || [ ! -f "$src" ]; then
  echo "publish-textfile: $src is not a regular file" >&2
  exit 1
fi

tmp=$(mktemp "$dir/.${prefix}XXXXXX")
trap 'rm -f "$tmp"' EXIT
# A series line is the name, then `{`, a space or the end of the line.
grep -E "^(# (HELP|TYPE) )?${prefix}[a-zA-Z0-9_]*([{ ]|\$)" <"$src" >"$tmp" || true
chmod 0644 "$tmp"
mv "$tmp" "$dir/${prefix%_}.prom"
trap - EXIT
