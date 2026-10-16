#!/bin/sh
# bootstrap-host.sh: put a host on the homelab apt repository and install its packages. The
# design is H3's *Steps, as PRs*, item 8 (docs/target-architecture/host/h3-pi-pilot.md). The
# step numbers are H7's, so H7 adds steps 3, 4 and 6 in place.
#
# Usage, as root, with homelab-archive-keyring.gpg beside this script:
#   ./bootstrap-host.sh <hostname>
#
# Exit codes: 0 installed; 1 a step failed, with the reason on stderr; 64 usage.
#
# The script runs on a host before anything homelab is installed, so it carries what it needs:
# the repository's address, not a name, because the LAN's DNS runs in the cluster; and the
# archive key's fingerprint. ansible/tests/repo/test_lan_facts_have_one_source.py holds the URL
# equal to daniel-box's server_ip, and the fingerprint equal to hosts/keys/'s keyring.
#
# HOMELAB_ROOT prefixes every path the script writes. Only the tests in hosts/lib/tests/ set it.

set -eu

REPO_URL=http://10.0.0.215:8791
ARCHIVE_FINGERPRINT=83B57416B342DDE1CBA0752D435D139ADFD6F016

ROOT=${HOMELAB_ROOT:-}
KEYRING=/usr/share/keyrings/homelab-archive-keyring.gpg
SOURCES=/etc/apt/sources.list.d/homelab.sources
# The update reads the homelab source alone, and List-Cleanup=0 keeps the other sources' lists,
# as the updater's own fetch does.
HOMELAB_APT="-o Dir::Etc::sourcelist=sources.list.d/homelab.sources -o Dir::Etc::sourceparts=- -o APT::Get::List-Cleanup=0"

DEBIAN_FRONTEND=noninteractive
export DEBIAN_FRONTEND

die() {
    echo "bootstrap-host: $*" >&2
    exit 1
}

if [ $# -ne 1 ]; then
    echo "usage: $0 <hostname>" >&2
    exit 64
fi
host=$1

# Step 1: refuse a host other than the one named, whose package would refuse it anyway.
actual=$(hostname)
[ "$actual" = "$host" ] || die "this host is $actual, not $host"

# Step 2: install the keyring once it carries the pinned key and nothing else, then the source.
keyring=$(dirname "$0")/homelab-archive-keyring.gpg
[ -r "$keyring" ] || die "cannot read $keyring"
gnupg_home=$(mktemp -d)
trap 'rm -rf "$gnupg_home"' EXIT
# A primary key's fingerprint is the first fpr record after its pub record. A keyring gpg
# cannot read yields no fingerprint, which the comparison below refuses.
fingerprints=$(gpg --homedir "$gnupg_home" --batch --with-colons --show-keys "$keyring" |
    awk -F: '$1 == "pub" { want = 1; next }
             $1 == "fpr" && want { print toupper($10); want = 0 }
             $1 == "sub" { want = 0 }')
[ "$fingerprints" = "$ARCHIVE_FINGERPRINT" ] ||
    die "$keyring holds [$fingerprints], not the archive key $ARCHIVE_FINGERPRINT"
install -D -m 0644 "$keyring" "$ROOT$KEYRING"
mkdir -p "$(dirname "$ROOT$SOURCES")"
cat > "$ROOT$SOURCES.tmp" <<EOF
# Written by bootstrap-host.sh. homelab-updater ships this file, and dpkg takes it over.
Types: deb
URIs: $REPO_URL
Suites: homelab
Components: main
Signed-By: $KEYRING
EOF
chmod 0644 "$ROOT$SOURCES.tmp"
mv "$ROOT$SOURCES.tmp" "$ROOT$SOURCES"

# Step 5: fetch the homelab source, failing on any fetch error, then install the two packages.
# $HOMELAB_APT is unquoted so that each option is its own argument.
# shellcheck disable=SC2086
apt-get update --error-on=any $HOMELAB_APT || die "apt-get update of the homelab source failed"
apt-get install -y homelab-updater "homelab-$host" ||
    die "apt-get install homelab-updater homelab-$host failed"
echo "bootstrap-host: installed homelab-updater and homelab-$host"
