# The test image for the host_packages job's container cases (H3, *CI (builds, never signs)*,
# step 4). CI builds it once per matrix leg and boots it with systemd as PID 1:
#
#   docker build --build-arg RELEASE=24.04 --build-context runner-ca=/etc/ssl/certs \
#     -t homelab-host-test:24.04 hosts/lib/tests/container
#
# scripts/validate/host_package_cases.py runs the cases against it.

ARG RELEASE=24.04

# Each release pinned by digest, so a rebuild starts from the same root filesystem. Bump both
# by hand, with SNAPSHOT below: renovate.json's ignorePaths holds `**/tests/**`, so no
# Renovate manager reads this file.
FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55 AS ubuntu-24.04
FROM ubuntu:26.04@sha256:f144425ff09be612d6d9ad965196e9cdc23dae1f42110a8a11a3e9a8198759f7 AS ubuntu-26.04

FROM ubuntu-${RELEASE}

# Ubuntu's snapshot archive at a pinned timestamp, so a rebuild resolves the same versions.
ARG SNAPSHOT=20261001T000000Z

ENV container=docker

# The snapshot archive redirects to https, and the stock image carries no CA bundle. The
# runner's bundle is mounted for this step's fetches only. The image keeps the pinned
# ca-certificates instead, for any fetch a case makes.
#
# docker-clean goes: it deletes downloaded debs after every apt run, and it sets an empty
# Dir::Cache::pkgcache, so the common-path case would compare a pkgcache.bin that never
# exists. The Ubuntu lists stay, so `tree`, which the Depends case's fixture needs, resolves
# with no fetch. Its deb is downloaded here too, because snapshot.ubuntu.com answers 5xx
# often enough to fail a case that fetches from it.
RUN --mount=type=bind,from=runner-ca,source=ca-certificates.crt,target=/run/runner-ca.crt \
    set -eu; \
    rm -f /etc/apt/apt.conf.d/docker-clean /etc/apt/sources.list; \
    . /etc/os-release; \
    printf '%s\n' \
        'Types: deb' \
        "URIs: https://snapshot.ubuntu.com/ubuntu/$SNAPSHOT" \
        "Suites: $VERSION_CODENAME $VERSION_CODENAME-updates" \
        'Components: main universe' \
        'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg' \
        > /etc/apt/sources.list.d/ubuntu.sources; \
    echo 'Acquire::Retries "10";' > /etc/apt/apt.conf.d/80retries; \
    apt-get -o Acquire::https::CaInfo=/run/runner-ca.crt update; \
    apt-get -o Acquire::https::CaInfo=/run/runner-ca.crt install -y --no-install-recommends \
        systemd systemd-sysv python3 ca-certificates; \
    apt-get install -y --no-install-recommends --download-only tree

# Units that would take dpkg's lock mid-case, or reach the runner's devices through
# --privileged, never start. The updater's own timer is left alone: no fixture ships its
# enable link, and the H3.6 case asserts it stays inactive.
RUN systemctl mask \
        apt-daily.timer apt-daily-upgrade.timer dpkg-db-backup.timer e2scrub_all.timer \
        fstrim.timer motd-news.timer \
        systemd-udevd.service systemd-udev-trigger.service systemd-udev-settle.service \
        systemd-udevd-control.socket systemd-udevd-kernel.socket \
        getty@tty1.service console-getty.service systemd-modules-load.service \
    && systemctl set-default multi-user.target

# The textfile directory with M3's owner and mode, which optimize_pi creates on the Pi
# (sys_user is ubuntu).
RUN install -d -o root -g ubuntu -m 2775 /var/lib/node-exporter-textfile

# dpkg unpacks the homelab.sources homelab-updater ships at the diverted path, outside
# sources.list.d/, so the test's source below survives every install. The source names no
# Signed-By, so the keyring homelab-updater ships from H3.4c is installed and unused.
RUN set -eu; \
    mkdir /var/lib/homelab-test; \
    dpkg-divert --local --no-rename \
        --divert /var/lib/homelab-test/homelab.sources.shipped \
        --add /etc/apt/sources.list.d/homelab.sources; \
    printf '%s\n' \
        'Types: deb' \
        'URIs: http://127.0.0.1:8791' \
        'Suites: homelab' \
        'Components: main' \
        'Trusted: yes' \
        > /etc/apt/sources.list.d/homelab.sources

STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
