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
from _helpers import REPO, SETUP_ROLES

ROLE = SETUP_ROLES / "hypervisor"
LIVE = ROLE / "files" / "staging-gate.pub"
RETIRED_DIR = ROLE / "files" / "staging-gate-retired"
INSTALL = ROLE / "tasks" / "install.yml"


def fingerprint(path: Path) -> str:
    """The key's SHA256 fingerprint.

    Never the key body — these are public halves, but the habit is what keeps a private one out of a
    log by accident.
    """
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


REL = str(LIVE.relative_to(REPO))


def _fingerprint_blob(blob: bytes) -> str:
    out = subprocess.run(
        ["ssh-keygen", "-lf", "/dev/stdin"],
        input=blob,
        capture_output=True,
        check=True,
    ).stdout.decode()
    return out.split()[1]


def historical_fingerprints() -> dict[str, str]:
    """Every fingerprint staging-gate.pub has held, keyed by short commit."""
    shas = subprocess.run(
        ["git", "-C", str(REPO), "log", "--format=%H", "--", REL],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return {
        sha[:8]: _fingerprint_blob(
            subprocess.run(
                ["git", "-C", str(REPO), "show", f"{sha}:{REL}"],
                capture_output=True,
                check=True,
            ).stdout
        )
        for sha in shas
    }


def unretired(retired_fingerprints: set[str]) -> dict[str, str]:
    live = fingerprint(LIVE)
    return {
        sha: fp
        for sha, fp in historical_fingerprints().items()
        if fp != live and fp not in retired_fingerprints
    }


def test_every_superseded_key_is_retired():
    """The retired directory must account for every generation git remembers.

    The 2026-08-29 rotation retired one key and left another authorized, because the
    retired directory was created empty and seeded with the generation being replaced.
    Every generation before that one was invisible to it -- a list that starts empty
    cannot know about the keys already on the host. Git does know, so ask git.
    """
    history = historical_fingerprints()
    assert history, f"git remembers no version of {REL}; this check cannot run"
    stray = unretired({fingerprint(p) for p in retired()})
    assert not stray, (
        "these superseded gate keys are in no retired file, so nothing withdraws them "
        f"and they stay in authorized_keys: {stray}"
    )


def test_an_unretired_key_is_flagged():
    # The rejecting half. Passing an empty retired set is the state the directory was in
    # before the first rotation, and every superseded generation must show up as stray --
    # otherwise the check above passes by finding nothing rather than by finding nothing
    # wrong. Fails if the key has never been rotated, which is the correct reading: there
    # would be nothing for this guard to catch.
    assert unretired(set()), (
        "no superseded key was detected even with an empty retired set, so "
        "test_every_superseded_key_is_retired cannot fail and proves nothing"
    )
