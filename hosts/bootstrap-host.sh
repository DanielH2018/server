#!/bin/sh
# bootstrap-host.sh: put a host on the homelab apt repository, install its packages, and run the
# one-time host steps. The design is H7's *`bootstrap-host.sh`, complete*
# (docs/target-architecture/host/h7-delete-ansible.md); H3's *Steps, as PRs*, item 8 first
# wrote it. The step numbers are H7's.
#
# Usage, as root, with homelab-archive-keyring.gpg beside this script:
#   ./bootstrap-host.sh <hostname> [--until <step>] [--local-build <clone>] [--offline <kit-dir>]
#   ./bootstrap-host.sh --help        print this header and exit
#
# Steps, in order. Each is idempotent, and --until <step> stops after one so a runbook can
# restore data before step 5 starts a service:
#   1  refuse a hostname other than <hostname>, or a host this script does not know
#   2  install the keyring once it carries the pinned key and nothing else, then homelab.sources
#      (--local-build installs the keyring only, because no repository is running yet)
#   3  create /etc/homelab/age.key when missing, and print its public key
#   4  the one-time host steps: the Pi's debloat purge; on a cluster node, the k3s binary
#   5  install homelab-tools, homelab-updater and homelab-<hostname>, then systemd-tmpfiles
#      --create (--local-build builds the three .deb files from the clone, then installs them)
#   6  the Pi only: start homelab-docker-engine.service once
#
# --local-build <clone> is daniel-box's way in, because it serves the repository it would
# install from. The script reads the clone with `git archive`, so it writes nothing under the
# clone's working tree. Everything else goes under one mktemp directory. HEAD must be on
# origin/master's first-parent chain with a green CI verdict (hosts/lib/bootstrap_anchor.py).
# --offline <kit-dir> reads every fetch from the break-glass kit (docs/break-glass.md, item 8),
# checks each file against the pin the download path checks, and makes neither GitHub read.
#
# daniel-server joins the cluster as an agent, so the operator passes K3S_URL and K3S_TOKEN in
# the environment (sudo --preserve-env). The script never prints or traces the token.
#
# Exit codes: 0 done; 1 a step failed, with the reason on stderr; 64 usage.
#
# The script runs on a host before anything homelab is installed, so it carries what it needs:
# the repository's address, not a name, because the LAN's DNS runs in the cluster; the archive
# key's fingerprint; and the pins of the tools it fetches. ansible/tests/repo/
# test_lan_facts_have_one_source.py holds the URL equal to daniel-box's server_ip and the
# fingerprint equal to hosts/keys/'s keyring. hosts/lib/tests/test_bootstrap_host_pins.py holds
# the nfpm, uv and k3s pins equal to their sources.
#
# HOMELAB_ROOT prefixes every path the script writes. Only the tests in hosts/lib/tests/ set it.

set -eu

for arg in "$@"; do
    case "$arg" in
        -h | --help)
            awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }' "$0"
            exit 0
            ;;
    esac
done

REPO_URL=http://10.0.0.215:8791
ARCHIVE_FINGERPRINT=83B57416B342DDE1CBA0752D435D139ADFD6F016
AGE_KEY=/etc/homelab/age.key
HOSTS="daniel-box daniel-server daniel-pi"
GITHUB_REPO=DanielH2018/server

# The k3s pin equals `k3s_version`, `k3s_install_script_url` and `k3s_install_script_sha256` in
# ansible/roles/k8s/system-upgrade/defaults/main.yml, so a rebuilt node never installs an older
# k3s than the SUC Plans target. Renovate bumps both files in one PR.
# renovate: datasource=github-releases depName=k3s-io/k3s
K3S_VERSION=v1.37.1+k3s1
K3S_INSTALL_URL=https://raw.githubusercontent.com/k3s-io/k3s/v1.37.1+k3s1/install.sh
K3S_INSTALL_SHA256=ed01f89fd977bf20ac1516bbebf8370bf3ddbaa55dac8aba610956a4c78cc00b

# The nfpm and uv pins are the blocks of hosts/packages/homelab-tools/package.yml, byte for byte,
# so the Renovate manager that bumps one copy bumps both. `--local-build` needs them before any
# package exists.
PINS=$(
    cat << 'EOF'
  nfpm:
    amd64:
      # renovate: datasource=github-release-attachments depName=goreleaser/nfpm
      version: v2.48.0
      sha256: 6d7f9a28b2f397934403950fcbba83f598b08d02edbe79a0815b3b4897341750
      url: https://github.com/goreleaser/nfpm/releases/download/{version}/nfpm_{bare}_Linux_x86_64.tar.gz
      member: nfpm
    arm64:
      # renovate: datasource=github-release-attachments depName=goreleaser/nfpm
      version: v2.48.0
      sha256: a9a75213763783b7669719e505474c748c5ad7dc66591a195619deb50f85c2f4
      url: https://github.com/goreleaser/nfpm/releases/download/{version}/nfpm_{bare}_Linux_arm64.tar.gz
      member: nfpm
  uv:
    amd64:
      # renovate: datasource=github-release-attachments depName=astral-sh/uv
      version: 0.13.4
      sha256: 2b428396f12572d4429cf210ca234dd599a33b3af0958a4d5ed569d63e3f344f
      url: https://github.com/astral-sh/uv/releases/download/{version}/uv-x86_64-unknown-linux-gnu.tar.gz
      member: uv-x86_64-unknown-linux-gnu/uv
    arm64:
      # renovate: datasource=github-release-attachments depName=astral-sh/uv
      version: 0.13.4
      sha256: 1ae72b4718729e2ccc4f1ae2c6d1e531ca8467800e0546e9130b3886023a60ab
      url: https://github.com/astral-sh/uv/releases/download/{version}/uv-aarch64-unknown-linux-gnu.tar.gz
      member: uv-aarch64-unknown-linux-gnu/uv
EOF
)

ROOT=${HOMELAB_ROOT:-}
KEYRING=/usr/share/keyrings/homelab-archive-keyring.gpg
SOURCES=/etc/apt/sources.list.d/homelab.sources
K3S_BIN=/usr/local/bin/k3s
SYSTEMD_DIR=/etc/systemd/system
# The update reads the homelab source alone, and List-Cleanup=0 keeps the other sources' lists,
# as the updater's own fetch does.
HOMELAB_APT="-o Dir::Etc::sourcelist=sources.list.d/homelab.sources -o Dir::Etc::sourceparts=- -o APT::Get::List-Cleanup=0"

DEBIAN_FRONTEND=noninteractive
export DEBIAN_FRONTEND

CLEAN=""
cleanup() {
    # shellcheck disable=SC2086
    [ -z "$CLEAN" ] || rm -rf $CLEAN
}
trap cleanup EXIT

die() {
    echo "bootstrap-host: $*" >&2
    exit 1
}

usage() {
    echo "usage: $0 <hostname> [--until <step>] [--local-build <clone>] [--offline <kit-dir>]" >&2
    exit 64
}

# Validate every argument and every precondition before step 1 writes anything.
host=""
until_step=6
clone=""
kit=""
while [ $# -gt 0 ]; do
    case "$1" in
        --until)
            [ $# -ge 2 ] || usage
            until_step=$2
            shift 2
            ;;
        --local-build)
            [ $# -ge 2 ] || usage
            clone=$2
            shift 2
            ;;
        --offline)
            [ $# -ge 2 ] || usage
            kit=$2
            shift 2
            ;;
        -*) usage ;;
        *)
            [ -z "$host" ] || usage
            host=$1
            shift
            ;;
    esac
done
[ -n "$host" ] || usage
case "$until_step" in
    1 | 2 | 3 | 4 | 5 | 6) ;;
    *) usage ;;
esac

known=no
for candidate in $HOSTS; do
    [ "$candidate" != "$host" ] || known=yes
done
[ "$known" = yes ] || die "$host is not a host this script knows: $HOSTS"

# The k3s mode follows the host, never K3S_URL.
case "$host" in
    daniel-box) k3s_mode=server k3s_unit=k3s.service k3s_other=k3s-agent.service ;;
    daniel-server) k3s_mode=agent k3s_unit=k3s-agent.service k3s_other=k3s.service ;;
    *) k3s_mode="" k3s_unit="" k3s_other="" ;;
esac
if [ "$k3s_mode" = agent ]; then
    [ -n "${K3S_URL:-}" ] || die "$host joins the cluster as an agent: K3S_URL is not set"
    [ -n "${K3S_TOKEN:-}" ] || die "$host joins the cluster as an agent: K3S_TOKEN is not set"
fi
if [ -n "$clone" ]; then
    [ -d "$clone/.git" ] || [ -f "$clone/.git" ] || die "--local-build: $clone is not a git clone"
fi
if [ -n "$kit" ]; then
    [ -d "$kit" ] || die "--offline: $kit is not a directory"
fi

# Step 1: refuse a host other than the one named, whose package would refuse it anyway.
actual=$(hostname)
[ "$actual" = "$host" ] || die "this host is $actual, not $host"

stop_after() {
    if [ "$until_step" -le "$1" ]; then
        echo "bootstrap-host: stopped after step $1 (--until $until_step)"
        exit 0
    fi
}
stop_after 1

# Step 2: install the keyring once it carries the pinned key and nothing else, then the source.
keyring=$(dirname "$0")/homelab-archive-keyring.gpg
[ -r "$keyring" ] || die "cannot read $keyring"
gnupg_home=$(mktemp -d)
CLEAN="$gnupg_home"
# A primary key's fingerprint is the first fpr record after its pub record. A keyring gpg
# cannot read yields no fingerprint, which the comparison below refuses.
fingerprints=$(gpg --homedir "$gnupg_home" --batch --with-colons --show-keys "$keyring" |
    awk -F: '$1 == "pub" { want = 1; next }
             $1 == "fpr" && want { print toupper($10); want = 0 }
             $1 == "sub" { want = 0 }')
[ "$fingerprints" = "$ARCHIVE_FINGERPRINT" ] ||
    die "$keyring holds [$fingerprints], not the archive key $ARCHIVE_FINGERPRINT"
install -D -m 0644 "$keyring" "$ROOT$KEYRING"
# The local build installs homelab-updater, which ships homelab.sources, so it writes none.
if [ -z "$clone" ]; then
    mkdir -p "$(dirname "$ROOT$SOURCES")"
    cat > "$ROOT$SOURCES.tmp" << EOF
# Written by bootstrap-host.sh. homelab-updater ships this file, and dpkg takes it over.
Types: deb
URIs: $REPO_URL
Suites: homelab
Components: main
Signed-By: $KEYRING
EOF
    chmod 0644 "$ROOT$SOURCES.tmp"
    mv "$ROOT$SOURCES.tmp" "$ROOT$SOURCES"
fi
stop_after 2

# Step 3: create the age key if missing, then print its public half. The key is written to a
# temporary name under umask 077 and renamed, so a failed age-keygen leaves no partial key that
# the next run would take for a real one. The private half is never printed.
if [ ! -s "$ROOT$AGE_KEY" ]; then
    command -v age-keygen > /dev/null || die "age-keygen is not installed, so cannot create $AGE_KEY"
    install -d -m 0755 "$(dirname "$ROOT$AGE_KEY")"
    rm -f "$ROOT$AGE_KEY.tmp"
    (
        umask 077
        age-keygen -o "$ROOT$AGE_KEY.tmp" > /dev/null 2>&1
    ) || die "age-keygen failed to create $AGE_KEY"
    mv "$ROOT$AGE_KEY.tmp" "$ROOT$AGE_KEY"
fi
chmod 0600 "$ROOT$AGE_KEY"
public=$(age-keygen -y "$ROOT$AGE_KEY") || die "cannot read a public key from $AGE_KEY"
echo "bootstrap-host: age public key for $host: $public"
stop_after 3

# --- helpers for steps 4 and 5 ---------------------------------------------------------------

arch=$(dpkg --print-architecture)
case "$arch" in
    amd64 | arm64) ;;
    *) die "no pin for architecture $arch" ;;
esac

# pin <tool> <arch> <key>: one field of a PINS block.
pin() {
    printf '%s\n' "$PINS" | awk -v t="$1" -v a="$2" -v k="$3" '
        /^  [a-z0-9]+:$/ { tool = $1; sub(":", "", tool); next }
        /^    [a-z0-9]+:$/ { cur = $1; sub(":", "", cur); next }
        tool == t && cur == a && $1 == k ":" { print $2; exit }'
}

# pin_url <tool>: the pinned URL with {version} and {bare} filled in.
pin_url() {
    version=$(pin "$1" "$arch" version)
    bare=${version#v}
    pin "$1" "$arch" url | sed -e "s/{version}/$version/g" -e "s/{bare}/$bare/g"
}

sha_of() {
    sha256sum "$1" | awk '{ print $1 }'
}

# fetch <url> <sha256> <dest>: download, or copy from the kit, then check the pin.
fetch() {
    name=${1##*/}
    if [ -n "$kit" ]; then
        [ -f "$kit/$name" ] || die "the offline kit has no $name"
        cp "$kit/$name" "$3"
    else
        curl -fsSL --retry 3 -o "$3" "$1" || die "cannot fetch $1"
    fi
    [ "$(sha_of "$3")" = "$2" ] || die "$name does not match its pinned sha256 $2"
}

# Step 4: the one-time host steps, each behind a guard that reads the state first.
if [ "$host" = daniel-pi ]; then
    purge=""
    for package in openvswitch-switch-dpdk openvswitch-switch openvswitch-common snapd fwupd; do
        if dpkg-query -W -f='${Status}' "$package" 2> /dev/null | grep -q 'install ok installed'; then
            purge="$purge $package"
        fi
    done
    if [ -n "$purge" ]; then
        # shellcheck disable=SC2086
        apt-get purge -y --autoremove $purge || die "apt-get purge$purge failed"
        echo "bootstrap-host: purged$purge"
    fi
    # A purge removes the unit files and leaves a failed unit pinned in memory.
    for unit in fwupd.service fwupd-refresh.service; do
        if systemctl is-failed --quiet "$unit"; then
            systemctl reset-failed "$unit"
        fi
    done
fi

if [ -n "$k3s_mode" ]; then
    installed=no
    if [ -x "$ROOT$K3S_BIN" ] && [ -e "$ROOT$SYSTEMD_DIR/$k3s_unit" ] &&
        "$ROOT$K3S_BIN" --version 2> /dev/null | grep -qF "$K3S_VERSION"; then
        installed=yes
    fi
    if [ "$installed" = yes ]; then
        echo "bootstrap-host: k3s $K3S_VERSION is installed, so the installer is skipped"
    else
        k3s_tmp=$(mktemp -d)
        CLEAN="$CLEAN $k3s_tmp"
        fetch "$K3S_INSTALL_URL" "$K3S_INSTALL_SHA256" "$k3s_tmp/install.sh"
        skip_download=""
        if [ -n "$kit" ]; then
            # install.sh fetches the binary from GitHub, so the kit's copy is placed first and
            # checked against the release's own checksum list.
            kit_bin=k3s
            [ "$arch" = arm64 ] && kit_bin=k3s-arm64
            kit_sums=sha256sum-$arch.txt
            [ -f "$kit/$kit_bin" ] || die "the offline kit has no $kit_bin"
            [ -f "$kit/$kit_sums" ] || die "the offline kit has no $kit_sums"
            want=$(awk -v f="$kit_bin" '$2 == f { print $1 }' "$kit/$kit_sums")
            [ -n "$want" ] || die "$kit_sums lists no $kit_bin"
            [ "$(sha_of "$kit/$kit_bin")" = "$want" ] || die "$kit_bin does not match $kit_sums"
            install -D -m 0755 "$kit/$kit_bin" "$ROOT$K3S_BIN"
            skip_download=true
        fi
        # The installer's mode is explicit, and K3S_URL and K3S_TOKEN reach it on an agent only.
        # Neither the token nor the environment is printed.
        if [ "$k3s_mode" = server ]; then
            env -u K3S_URL -u K3S_TOKEN \
                INSTALL_K3S_VERSION="$K3S_VERSION" INSTALL_K3S_EXEC=server \
                INSTALL_K3S_SKIP_ENABLE=true INSTALL_K3S_SKIP_START=true \
                INSTALL_K3S_SKIP_DOWNLOAD="$skip_download" \
                sh "$k3s_tmp/install.sh" || die "the k3s installer failed"
        else
            INSTALL_K3S_VERSION="$K3S_VERSION" INSTALL_K3S_EXEC=agent \
                INSTALL_K3S_SKIP_ENABLE=true INSTALL_K3S_SKIP_START=true \
                INSTALL_K3S_SKIP_DOWNLOAD="$skip_download" \
                sh "$k3s_tmp/install.sh" || die "the k3s installer failed"
        fi
    fi
    # Exactly one k3s unit exists, the one this host's role names.
    [ -e "$ROOT$SYSTEMD_DIR/$k3s_unit" ] || die "$k3s_unit does not exist after the installer"
    [ ! -e "$ROOT$SYSTEMD_DIR/$k3s_other" ] || die "$k3s_other exists on a $k3s_mode node"
fi
stop_after 4

# Step 5: install the three packages, then create each directory a package declares in tmpfiles.d.
# $HOMELAB_APT is unquoted so that each option is its own argument.
if [ -z "$clone" ]; then
    # shellcheck disable=SC2086
    apt-get update --error-on=any $HOMELAB_APT || die "apt-get update of the homelab source failed"
    apt-get install -y homelab-tools || die "apt-get install homelab-tools failed"
    apt-get install -y homelab-updater "homelab-$host" ||
        die "apt-get install homelab-updater homelab-$host failed"
else
    work=$(mktemp -d)
    CLEAN="$CLEAN $work"
    mkdir -p "$work/bin" "$work/tree" "$work/debs" "$work/uv-cache" "$work/tools-cache"
    command -v git > /dev/null || die "--local-build needs git"

    for tool in nfpm uv; do
        fetch "$(pin_url "$tool")" "$(pin "$tool" "$arch" sha256)" "$work/$tool.tgz"
        tar -xzf "$work/$tool.tgz" -C "$work" "$(pin "$tool" "$arch" member)" ||
            die "cannot extract $tool from its archive"
        member=$(pin "$tool" "$arch" member)
        install -m 0755 "$work/$member" "$work/bin/$tool"
    done

    # git archive reads the clone and writes nothing into it.
    sha=$(git -C "$clone" rev-parse HEAD) || die "cannot read HEAD of $clone"
    git -C "$clone" archive HEAD | tar -x -C "$work/tree" || die "cannot archive HEAD of $clone"
    python_version=$(sed -n 's/^host_python_version: "\(.*\)"$/\1/p' \
        "$work/tree/ansible/inventory/group_vars/all.yml")
    [ -n "$python_version" ] || die "cannot read host_python_version from the clone"

    UV_CACHE_DIR=$work/uv-cache
    export UV_CACHE_DIR
    uv_run="$work/bin/uv run --frozen --python $python_version"
    if [ -n "$kit" ]; then
        [ -d "$kit/uv-cache" ] && cp -R "$kit/uv-cache/." "$work/uv-cache/"
        [ -d "$kit/tools-cache" ] && cp -R "$kit/tools-cache/." "$work/tools-cache/"
        if [ -d "$kit/uv-python" ]; then
            UV_PYTHON_INSTALL_DIR=$kit/uv-python
            export UV_PYTHON_INSTALL_DIR
        fi
        UV_OFFLINE=1
        export UV_OFFLINE
        uv_run="$uv_run --no-python-downloads --offline"
        echo "bootstrap-host: --offline: the clone at $sha is unchecked; the operator who chose it is its trust root"
    else
        # shellcheck disable=SC2086
        (cd "$work/tree" && $uv_run python hosts/lib/bootstrap_anchor.py "$clone" "$sha" \
            --repo "$GITHUB_REPO") || die "the clone's HEAD $sha is not a green first-parent commit of master"
    fi

    # shellcheck disable=SC2086
    (cd "$work/tree" && $uv_run python scripts/validate/host_packages.py render --tree . \
        --out "$work/render") || die "the host package render failed"
    # shellcheck disable=SC2086
    (cd "$work/tree" && $uv_run python scripts/validate/host_packages.py fetch-tools \
        "$work/render" --cache "$work/tools-cache") || die "a homelab-tools pin did not verify"

    count=$(git -C "$clone" rev-list --count --first-parent HEAD)
    HOMELAB_VERSION="0.$count+g$(printf '%s' "$sha" | cut -c1-12)"
    export HOMELAB_VERSION
    for dir in "$work/render/packages/homelab-tools" "$work/render/packages/homelab-updater" \
        "$work/render/hosts/$host"; do
        [ -f "$dir/nfpm.yaml" ] || die "the render holds no $dir/nfpm.yaml"
        (cd "$dir" && "$work/bin/nfpm" package --config nfpm.yaml --packager deb \
            --target "$work/debs/") || die "nfpm failed for $dir"
    done

    # The whole sources.list.d is read, so apt resolves the packages' Depends against Ubuntu's
    # archive. Offline, a source on a GitHub host is left out, and the next online updater
    # run reads it again.
    update_dir=/etc/apt/sources.list.d
    if [ -n "$kit" ]; then
        update_dir=$work/sources.list.d
        mkdir -p "$update_dir"
        for source in "$ROOT"/etc/apt/sources.list.d/*; do
            [ -e "$source" ] || continue
            case "${source##*/}" in
                *github-cli*) ;;
                *) cp "$source" "$update_dir/" ;;
            esac
        done
    fi
    apt-get update -o "Dir::Etc::sourceparts=$update_dir" ||
        die "apt-get update failed; the .deb Depends need Ubuntu's archive"
    # One install, so apt resolves the three packages' Depends among themselves.
    set -- "$work"/debs/homelab-tools_*.deb "$work"/debs/homelab-updater_*.deb \
        "$work/debs/homelab-$host"_*.deb
    apt-get install -y "$@" || die "apt-get install of the local build failed"
fi
systemd-tmpfiles --create || die "systemd-tmpfiles --create failed"
echo "bootstrap-host: installed homelab-tools, homelab-updater and homelab-$host"
stop_after 5

# Step 6: the Pi only. The package ships the engine unit as a manual one, so nothing else starts it.
if [ "$host" = daniel-pi ]; then
    if systemctl cat homelab-docker-engine.service > /dev/null 2>&1; then
        systemctl start homelab-docker-engine.service ||
            die "homelab-docker-engine.service failed to start"
    else
        echo "bootstrap-host: homelab-docker-engine.service is not installed, so step 6 starts nothing"
    fi
fi
