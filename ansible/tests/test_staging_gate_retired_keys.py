"""The retired staging-gate keys are withdrawn, and can never also be the live one.

`roles/setup/hypervisor` authorizes `files/staging-gate.pub` with `state: present` and
`exclusive` left false, so it only ever ADDS. Swapping that file is therefore half a
rotation: the previous public half stays in `authorized_keys` and the old key keeps
working. `files/staging-gate-retired/*.pub` is the other half, removed with `state: absent`.

The first rotation (2026-08-29) was forced by the private half reaching a Claude transcript
in plaintext, which is precisely the case where the old key must stop being *accepted*
rather than merely stop being used.

The failure this file is built to catch is the quiet one: a rotation that copies the new
key into the retired directory, or forgets to retire the old one, reads as a completed
rotation from every other angle.
"""

import subprocess
from pathlib import Path

import pytest
import yaml

ROLE = Path(__file__).resolve().parents[1] / "roles" / "setup" / "hypervisor"
LIVE = ROLE / "files" / "staging-gate.pub"
RETIRED_DIR = ROLE / "files" / "staging-gate-retired"
INSTALL = ROLE / "tasks" / "install.yml"


def fingerprint(path: Path) -> str:
    """The key's SHA256 fingerprint. Never the key body — these are public halves, but the
    habit is what keeps a private one out of a log by accident."""
    out = subprocess.run(
        ["ssh-keygen", "-lf", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return out.split()[1]


def retired() -> list[Path]:
    return sorted(RETIRED_DIR.glob("*.pub")) if RETIRED_DIR.is_dir() else []


def test_live_public_key_exists_and_parses():
    assert LIVE.is_file(), f"{LIVE} is missing"
    assert fingerprint(LIVE).startswith("SHA256:")


def test_live_key_ends_with_a_newline():
    # PR #607 landed because a key written one byte short made OpenSSH refuse it. The
    # private half is the one that bit, but a .pub without a trailing newline concatenates
    # onto the next authorized_keys line and silently authorizes nothing.
    assert LIVE.read_bytes().endswith(b"\n"), f"{LIVE} needs a trailing newline"


@pytest.mark.parametrize("path", retired(), ids=lambda p: p.name)
def test_retired_key_parses_and_ends_with_a_newline(path: Path):
    assert fingerprint(path).startswith("SHA256:")
    assert path.read_bytes().endswith(b"\n"), f"{path} needs a trailing newline"


@pytest.mark.parametrize("path", retired(), ids=lambda p: p.name)
def test_retired_key_is_not_the_live_key(path: Path):
    # The rejecting half. A rotation that copied the NEW key into the retired directory
    # would deauthorize the key it had just authorized -- and the two tasks are `present`
    # then `absent` on the same user, so the removal wins and the gate loses its access
    # with every file in the tree looking correct.
    assert fingerprint(path) != fingerprint(LIVE), (
        f"{path.name} has the same fingerprint as the live key; the role would authorize "
        "it and then immediately withdraw it"
    )


def test_install_withdraws_every_retired_key():
    """A retired key nothing removes is a key still accepted."""
    tasks = yaml.safe_load(INSTALL.read_text())
    withdrawals = [
        t
        for t in tasks
        if isinstance(t, dict)
        and "ansible.posix.authorized_key" in t
        and t["ansible.posix.authorized_key"].get("state") == "absent"
    ]
    assert withdrawals, (
        "install.yml has no authorized_key task with state: absent, so nothing withdraws a "
        "retired key and rotating staging-gate.pub leaves the old one working"
    )
    globs = " ".join(str(w.get("with_fileglob", "")) for w in withdrawals)
    assert "staging-gate-retired" in globs, (
        "the withdrawal task no longer reads files/staging-gate-retired/*.pub"
    )
