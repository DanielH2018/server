"""The by-hand case's check of what systemd-sysctl's boot path reads, without a container.

The case runs `systemd-sysctl --cat-config` in CI's `host_packages` job. These tests feed the
check that output's shape: a `# <path>` line opens each file, in the order systemd-sysctl
applies them.

Run: uv run pytest scripts/validate/tests/test_host_container_boot_sysctl.py
"""

import pytest

from validate.validate_lib import host_container

HOST_TUNING = "# /etc/sysctl.d/90-homelab-host-tuning.conf\nkernel.sysrq = 176\n"
PI_TUNING = "# /etc/sysctl.d/90-homelab-pi-tuning.conf\nvm.swappiness=130\n"
DISTRO = (
    "# /usr/lib/sysctl.d/55-kernel-hardening.conf\n"
    "# /proc/modules, etc), and this setting can censor the addresses.\n"
    "kernel.kptr_restrict = 1\n"
)
SYSCTL_LINK = "# /etc/sysctl.d/99-sysctl.conf\n#vm.swappiness=10\n"


def test_a_cat_config_that_reads_the_pi_files_is_clean():
    cat_config = "\n".join([DISTRO, HOST_TUNING, PI_TUNING, SYSCTL_LINK])
    assert host_container.boot_sysctl_problems(cat_config) == []


@pytest.mark.parametrize(
    ("parts", "problem"),
    [
        (
            [DISTRO, HOST_TUNING, "# /etc/sysctl.conf\nvm.swappiness=130\n"],
            "does not read /etc/sysctl.d/90-homelab-pi-tuning.conf",
        ),
        (
            [
                DISTRO,
                HOST_TUNING,
                "# /etc/sysctl.d/90-homelab-pi-tuning.conf\n",
                "# /etc/sysctl.d/99-sysctl.conf\nvm.swappiness=130\n",
            ],
            "from /etc/sysctl.d/99-sysctl.conf",
        ),
        (
            [
                DISTRO,
                HOST_TUNING,
                PI_TUNING,
                "# /etc/sysctl.d/99-sysctl.conf\nvm.swappiness = 10\n",
            ],
            "vm.swappiness=10",
        ),
    ],
    ids=["pi-file-unread", "key-in-sysctl-conf", "key-overridden-later"],
)
def test_a_cat_config_missing_a_pi_file_or_key_is_flagged(parts, problem):
    problems = host_container.boot_sysctl_problems("\n".join(parts))
    assert problem in "; ".join(problems)
