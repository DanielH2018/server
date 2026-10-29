#!/usr/bin/env python3
"""The container case for `hosts/bootstrap-host.sh`'s repository path, against real apt (#6245).

Without `--local-build` the script installs from the homelab repository: step 2 checks the
keyring beside it against ARCHIVE_FINGERPRINT and writes homelab.sources at REPO_URL, and step 5
runs `apt-get update --error-on=any` of that source alone, then installs the three packages. A
test repository cannot be signed with the archive key, whose private half lives only on
daniel-box, so this case runs a scratch copy of the script with those two lines rewritten:

  * REPO_URL becomes the test repository that `host_fixtures.publish` signs with its throwaway
    key and the container serves on 127.0.0.1:8791;
  * ARCHIVE_FINGERPRINT becomes that key's fingerprint, read from the exported keyring, which
    sits beside the copy as homelab-archive-keyring.gpg.

Each rewrite must match exactly one line, so a rename of either variable fails the case rather
than leaving the real value in place. The rest of the script runs unmodified on a container
named daniel-pi, the host the repository path is for, with no k3s step. The repository serves
the job's head builds of homelab-tools, homelab-updater and the real homelab-daniel-pi.

`bootstrap_case.py`'s apt-get shim records each call and turns `install` into
`install --simulate`, so apt resolves the packages from the signed index and the image's Ubuntu
lists while no maintainer script runs. The case asserts the one update of the homelab source,
the two installs by name, and apt's `Inst` line for each package. `Cases.case` adds that the
server log holds a GET of the suite's InRelease and that `/var/lib/apt/lists/` holds a list for
127.0.0.1:8791, which together show that the REPO_URL rewrite took effect.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import functools
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from validate.validate_lib.bootstrap_case import SCRIPT, install_apt_shim, APT_CALLS
from validate.validate_lib.host_container import PORT, CaseFailed, Container, expect
from validate.validate_lib.host_fixtures import TEST_KEYRING

HOST = "daniel-pi"
NAME = "bootstrap daniel-pi from a signed repository"
REPO_URL = f"http://127.0.0.1:{PORT}"
SCRATCH = "/srv/bootstrap-repo"
INSTALLED = (
    f"bootstrap-host: installed homelab-tools, homelab-updater and homelab-{HOST}"
)


def rewrite(script: str, url: str, fingerprint: str) -> str:
    """The script with its REPO_URL and ARCHIVE_FINGERPRINT lines set to `url` and `fingerprint`.

    Raises:
        CaseFailed: A variable is set on no line, or on more than one.
    """
    for name, value in (("REPO_URL", url), ("ARCHIVE_FINGERPRINT", fingerprint)):
        script, count = re.subn(
            rf"^{name}=.*$", f"{name}={value}", script, flags=re.MULTILINE
        )
        expect(count == 1, f"bootstrap-host.sh sets {name} on {count} lines, not one")
    return script


def primary_fingerprints(colons: str) -> list[str]:
    """Each primary key's fingerprint in `gpg --with-colons` output, as the script's awk reads it.

    A primary key's fingerprint is the first `fpr` record after its `pub` record; a subkey's
    `fpr` follows a `sub` record and is skipped.
    """
    found, want = [], False
    for record in colons.splitlines():
        fields = record.split(":")
        if fields[0] == "pub":
            want = True
        elif fields[0] == "fpr" and want:
            found.append(fields[9].upper())
            want = False
        elif fields[0] == "sub":
            want = False
    return found


def key_fingerprint(keyring: Path) -> str:
    """The fingerprint of the one primary key in `keyring`.

    Raises:
        CaseFailed: gpg cannot read the keyring, or it holds no primary key or more than one.
    """
    with tempfile.TemporaryDirectory() as home:
        shown = subprocess.run(
            ["gpg", "--homedir", home, "--batch", "--with-colons", "--show-keys"]
            + [str(keyring)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    expect(shown.returncode == 0, f"gpg cannot read {keyring}: {shown.stderr.strip()}")
    fingerprints = primary_fingerprints(shown.stdout)
    expect(len(fingerprints) == 1, f"{keyring} holds keys {fingerprints}, not one")
    return fingerprints[0]


def apt_problems(host: str, calls: str, stdout: str) -> list[str]:
    """What is wrong with step 5's repository install, from the shim's log and the script's output.

    Args:
        host: The host the script ran for.
        calls: The shim's log, one apt-get argument list per line.
        stdout: The script's standard output, which holds apt's simulations.
    """
    lines = [line.split() for line in calls.splitlines()]
    updates = [line for line in lines if line[:1] == ["update"]]
    problems = []
    if len(updates) != 1 or "--error-on=any" not in updates[0]:
        problems.append(f"apt-get ran updates {updates}, not one with --error-on=any")
    elif "Dir::Etc::sourcelist=sources.list.d/homelab.sources" not in updates[0]:
        problems.append(f"the update {updates[0]} does not read homelab.sources alone")
    installs = [line for line in lines if line[:1] == ["install"]]
    want = [
        ["install", "-y", "homelab-tools"],
        ["install", "-y", "homelab-updater", f"homelab-{host}"],
    ]
    if installs != want:
        problems.append(f"apt-get ran installs {installs}, not {want}")
    problems += [
        f"apt's simulation installs no {package}"
        for package in ("homelab-tools", "homelab-updater", f"homelab-{host}")
        if not re.search(rf"^Inst {re.escape(package)} ", stdout, re.MULTILINE)
    ]
    return problems


def _body(cases, c: Container, scratch: Path) -> None:
    expect(bool(cases.tools), "the head build holds no homelab-tools to serve")
    cases.serving("updater-head", "pi-head", *cases.tools)
    keyring = cases.work / "repo" / TEST_KEYRING
    script = rewrite(SCRIPT.read_text(), REPO_URL, key_fingerprint(keyring))
    (scratch / "bootstrap-host.sh").write_text(script)
    shutil.copy(keyring, scratch / "homelab-archive-keyring.gpg")
    install_apt_shim(c)
    result = c.sh(f"sh {SCRATCH}/bootstrap-host.sh {HOST}", 1800)
    tail = (result.stdout + result.stderr)[-3000:]
    expect(result.returncode == 0, f"the script exited {result.returncode}:\n{tail}")
    expect(INSTALLED in result.stdout, f"the output holds no {INSTALLED!r}:\n{tail}")
    problems = apt_problems(HOST, c.read(APT_CALLS), result.stdout)
    expect(not problems, "; ".join(problems))


def run(cases) -> None:
    """Run the repository case, and record its verdict on `cases`."""
    scratch = cases.work / "bootstrap-repo"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    c = Container(
        cases.image,
        "homelab-host-test-bootstrap-repo",
        cases.work,
        hostname=HOST,
        mounts=(f"{scratch}:{SCRATCH}:ro",),
    )
    try:
        c.boot(cases.python)
        c.serve()
    except (CaseFailed, subprocess.SubprocessError) as exc:
        cases.failed.append(NAME)
        print(f"[FAIL] {c.name}: {NAME}: {exc}", file=sys.stderr)
        c.remove()
        return
    try:
        cases.case(c, NAME, functools.partial(_body, cases, c, scratch))
    finally:
        c.remove()
