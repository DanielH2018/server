"""Host state the real Pi has and the systemd test container lacks (H7.27).

The Pi's package edits conffiles of `ufw` (`/etc/ufw/sysctl.conf`) and reloads `ssh.service`
after `sshd -t`. The container installs neither package, so reconcile's line arm fails on the
missing directory and `reload:ssh.service` fails its validation. The seed gives the container
the stock file and an `sshd` that accepts any config, which is what the arm and the validation
meet on the real Pi. The arm and the validation stay strict.
"""

UFW_SYSCTL = "/etc/ufw/sysctl.conf"
SSHD = "/usr/sbin/sshd"
# The two lines Ubuntu ships uncommented, which the Pi's package rewrites to 1.
UFW_STOCK = "net/ipv4/conf/default/log_martians=0\\nnet/ipv4/conf/all/log_martians=0\\n"


def seed_pi_host(c) -> None:
    """Create ufw's stock sysctl file and a stub `sshd` in container `c`, when absent."""
    c.ok(
        f"mkdir -p /etc/ufw && [ -e {UFW_SYSCTL} ] || printf '{UFW_STOCK}' > {UFW_SYSCTL}"
    )
    c.ok(
        f"[ -e {SSHD} ] || printf '#!/bin/sh\\nexit 0\\n' > {SSHD} && chmod 0755 {SSHD}"
    )
