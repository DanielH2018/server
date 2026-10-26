"""Host state the real Pi has and the systemd test container lacks (H7.27, H7.25).

The Pi's package edits conffiles of `ufw` (`/etc/ufw/sysctl.conf`) and reloads `ssh.service`
after `sshd -t`. The container installs neither package, so reconcile's line arm fails on the
missing directory and `reload:ssh.service` fails its validation. The seed gives the container
the stock file and an `sshd` that accepts any config, which is what the arm and the validation
meet on the real Pi. The arm and the validation stay strict.

H7.25 adds four more. The Pi's package edits `/etc/hosts` and ships `/etc/resolv.conf`, and the
container runtime bind-mounts both, so a rename onto either fails with EBUSY until they are
unmounted. The package restarts `auditd.service` after it caps the log, and the container has no
auditd, so a stub unit stands in. The node exporter unit listens on the Pi's LAN address, which
the container lacks, so a drop-in swaps its `ExecStart=` for `sleep`; the unit's tmpfiles
`ExecStartPre=` still runs. The Pi already holds the `node_exporter` user, which the users arm
adopts by uid.
"""

UFW_SYSCTL = "/etc/ufw/sysctl.conf"
SSHD = "/usr/sbin/sshd"
# The two lines Ubuntu ships uncommented, which the Pi's package rewrites to 1.
UFW_STOCK = "net/ipv4/conf/default/log_martians=0\\nnet/ipv4/conf/all/log_martians=0\\n"
AUDITD_STUB = "[Service]\\nType=oneshot\\nRemainAfterExit=yes\\nExecStart=/bin/true\\n"
EXPORTER_DROPIN = "[Service]\\nExecStart=\\nExecStart=/bin/sleep infinity\\n"


def seed_pi_host(c) -> None:
    """Seed the Pi's host state in container `c`: ufw, sshd, auditd, the exporter and two mounts."""
    c.ok(
        f"mkdir -p /etc/ufw && [ -e {UFW_SYSCTL} ] || printf '{UFW_STOCK}' > {UFW_SYSCTL}"
    )
    c.ok(
        f"[ -e {SSHD} ] || printf '#!/bin/sh\\nexit 0\\n' > {SSHD} && chmod 0755 {SSHD}"
    )
    c.ok(
        "umount /etc/hosts 2>/dev/null || true; umount /etc/resolv.conf 2>/dev/null || true"
    )
    c.ok(f"printf '{AUDITD_STUB}' > /etc/systemd/system/auditd.service")
    c.ok("mkdir -p /etc/systemd/system/node_exporter.service.d")
    c.ok(
        f"printf '{EXPORTER_DROPIN}' > /etc/systemd/system/node_exporter.service.d/10-test.conf"
    )
    c.ok("systemctl daemon-reload")
