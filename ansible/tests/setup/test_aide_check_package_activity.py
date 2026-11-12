"""Guards on the AIDE check's explanations that read what a package run did (#7919).

The classifier credits a package's own activity in the window: an install or upgrade explains
an owner, group or mode change on a path it owns, a dpkg event explains a distro link it left
dangling inside the system trees, the host package's links into its secrets tmpfs are its own,
and a grub-efi or shim run explains the EFI loaders. Each is a clean/flagged pair, run against
the fake dpkg database `_aide_check_run._fake_host` writes.

Run: uv run pytest ansible/tests/setup/test_aide_check_package_activity.py
"""

import pytest
from _aide_check_run import (
    CHANGED_CONTENT,
    CHANGED_MODE,
    LINK_ADDED,
    LINK_CHANGED,
    LINK_CHANGED_MODE,
    SECRET_LINK,
    SECRET_TARGET,
    SECRETS_MANIFEST,
    SYSTEM_SECRET_LINK,
    _report,
    _run_check,
)

EFI_LOADER = "/boot/efi/EFI/ubuntu/shimx64.efi"
# (entry, the `_run_check` setup before the run).
EXPLAINED = {
    "a distro link into a package that is not installed, in a week its owner ran": (
        f"{LINK_ADDED}: /usr/src/tool-headers/rust",
        {"packages_ran": ("tool",)},
    ),
    "the host package's link into its secrets tmpfs": (
        f"{LINK_CHANGED}: {SECRET_LINK}",
        {},
    ),
    "a package file whose upgrade changed its mode": (
        f"{CHANGED_MODE}: /usr/bin/tool",
        {"packages_upgraded": ("tool",)},
    ),
    "the host package's /usr/lib link into its secrets tmpfs": (
        f"{LINK_CHANGED}: {SYSTEM_SECRET_LINK}",
        {},
    ),
    "a secret file the host package's upgrade turned into a link": (
        f"{LINK_CHANGED_MODE}: {SECRET_LINK}",
        {"packages_upgraded": ("homelab-host",)},
    ),
    "an EFI loader grub-install wrote from a shim upgrade": (
        f"{CHANGED_CONTENT}: {EFI_LOADER}",
        {"packages_ran": ("shim-signed",)},
    ),
}
NOT_THE_LINK = "a package owns it, but it is not the link the package installed"
MODE = "owner, group or permissions changed"
# (entry, the `_run_check` setup before the run, the reason the journal and the tile give).
FLAGGED = {
    "a mode change in a week only another package upgraded": (
        f"{CHANGED_MODE}: /usr/bin/tool",
        {"packages_upgraded": ("other",)},
        MODE,
    ),
    "an upgrade-week mode change on content that differs from the package": (
        f"{CHANGED_MODE}: /usr/bin/tool",
        {"packages_upgraded": ("tool",), "rewrite": {"/usr/bin/tool": "trojan\n"}},
        "differs from its package copy",
    ),
    "a mode change on the host package's apt hook in a week it upgraded": (
        f"{CHANGED_MODE}: /etc/apt/apt.conf.d/91homelab-hook",
        {"packages_upgraded": ("homelab-host",)},
        MODE,
    ),
    "a mode change on a host package script in a week it upgraded": (
        f"{CHANGED_MODE}: /usr/lib/homelab/check",
        {"packages_upgraded": ("homelab-host",)},
        MODE,
    ),
    "a dangling package link in a week its package did not run": (
        f"{LINK_ADDED}: /usr/src/tool-headers/rust",
        {"packages_ran": ("other",)},
        NOT_THE_LINK,
    ),
    "a dangling package link outside the system trees in a week its package ran": (
        f"{LINK_CHANGED}: /usr/bin/tool-link",
        {"relink": {"/usr/bin/tool-link": "/tmp/missing/tool"}},
        NOT_THE_LINK,
    ),
    "a dangling package link into /usr/local in a week its package ran": (
        f"{LINK_CHANGED}: /usr/bin/tool-link",
        {"relink": {"/usr/bin/tool-link": "/usr/local/bin/missing"}},
        NOT_THE_LINK,
    ),
    "another package's link into the secrets tmpfs": (
        f"{LINK_CHANGED}: /usr/bin/tool-link",
        {"relink": {"/usr/bin/tool-link": SECRET_TARGET}},
        NOT_THE_LINK,
    ),
    "a host package script turned into a link into the secrets tmpfs": (
        f"{LINK_CHANGED}: /usr/lib/homelab/check",
        {"relink": {"/usr/lib/homelab/check": SECRET_TARGET}},
        NOT_THE_LINK,
    ),
    "a host package apt hook turned into a link into the secrets tmpfs": (
        f"{LINK_CHANGED}: /etc/apt/apt.conf.d/91homelab-hook",
        {"relink": {"/etc/apt/apt.conf.d/91homelab-hook": SECRET_TARGET}},
        NOT_THE_LINK,
    ),
    "a host package script linked to its own path in the secrets tmpfs, undeclared": (
        f"{LINK_CHANGED}: /usr/lib/homelab/check",
        {
            "relink": {
                "/usr/lib/homelab/check": "/run/homelab-secrets/usr/lib/homelab/check"
            }
        },
        NOT_THE_LINK,
    ),
    "the host package's link pointed at another output in its secrets tmpfs": (
        f"{LINK_CHANGED}: {SYSTEM_SECRET_LINK}",
        {"relink": {SYSTEM_SECRET_LINK: SECRET_TARGET}},
        NOT_THE_LINK,
    ),
    "the host package's link while its secrets manifest is unreadable": (
        f"{LINK_CHANGED}: {SYSTEM_SECRET_LINK}",
        {"rewrite": {SECRETS_MANIFEST: "not json"}},
        NOT_THE_LINK,
    ),
    "the host package's link pointed outside its secrets tmpfs": (
        f"{LINK_CHANGED}: {SECRET_LINK}",
        {"relink": {SECRET_LINK: "/run/other/config.env"}},
        NOT_THE_LINK,
    ),
    "an EFI loader in a week only a kernel package ran": (
        f"{CHANGED_CONTENT}: {EFI_LOADER}",
        {"packages_ran": ("linux-image-7.0.0-42-generic",)},
        "regenerated by package triggers, but dpkg.log holds no package change since the "
        "baseline that explains it",
    ),
}


@pytest.mark.parametrize(("entry", "setup"), EXPLAINED.values(), ids=EXPLAINED.keys())
def test_a_change_its_package_run_made_is_clean(tmp_path, entry, setup):
    code, pushed, _, _ = _run_check(tmp_path, _report(entry), rc=4, **setup)
    assert pushed[0] == "up", pushed
    assert code == 0


@pytest.mark.parametrize(
    ("entry", "setup", "reason"), FLAGGED.values(), ids=FLAGGED.keys()
)
def test_a_change_no_package_run_explains_is_flagged(tmp_path, entry, setup, reason):
    code, pushed, _, _ = _run_check(tmp_path, _report(entry), rc=4, **setup)
    path = entry.split(": ", 1)[1]
    assert pushed[0] == "down", pushed
    assert f"{path} ({reason})" in pushed[1], pushed[1]
    assert code == 1
