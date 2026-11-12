#!/usr/bin/env python3
"""The container case for step 3 of `hosts/bootstrap-host.sh` on an image with no age.

The other bootstrap cases never take step 3's install: the test image preinstalls `age`, and they
turn every `apt-get install` into a simulation. This case purges `age` and the package lists, and
runs the script through `--until 3` with real apt, so it proves the package name, that the
package ships `age-keygen`, and that the update survives a dead source. The script's REPO_URL
points at a closed port, the way step 2's source reads while the homelab repository is down.

The fetch goes to snapshot.ubuntu.com, which answers 5xx often enough to redden PRs (#8041). A
run that dies at the age install while apt shows that mirror answering 5xx, or not answering at
all, gives no verdict rather than a failure, and `host_package_cases.py` counts it in its last
line. A 404 still fails: it means the pinned snapshot is wrong. So does a run that dies anywhere
else, such as on a package that ships no `age-keygen`.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import re
import shutil
import subprocess
import sys

from validate.validate_lib.bootstrap_case import SCRIPT, output_problems, script_value
from validate.validate_lib.bootstrap_repo_case import rewrite
from validate.validate_lib.host_container import CaseFailed, Container, expect

HOST = "daniel-pi"
NAME = "step 3 installs age on an image with none"
SCRATCH = "/srv/bootstrap-age"
DEAD_REPO = "http://127.0.0.1:9"
KEYRING = SCRIPT.parent / "keys" / "homelab-archive-keyring.gpg"
MARKERS = ("bootstrap-host: installed age", f"age public key for {HOST}: age1")
# apt's `Failed to fetch <url>  <status>` line for the snapshot mirror, on a 5xx or no answer.
MIRROR_DOWN = re.compile(
    r"Failed to fetch https?://snapshot\.ubuntu\.com/\S*\s+"
    r"(?:5\d\d\b|Could not connect|Could not resolve|Temporary failure resolving)"
)
INSTALL_DIED = "bootstrap-host: cannot install age,"


class MirrorDown(Exception):
    """snapshot.ubuntu.com failed the case's fetch, so the run says nothing about the script."""


def expect_age_from_apt(c: Container, python: str) -> None:
    """Purge `age` and the lists in `c`, run the script through step 3, and read its output.

    Raises:
        CaseFailed: The run failed, or its output lacks a marker.
        MirrorDown: The age install failed while snapshot.ubuntu.com answered 5xx or not
            at all.
    """
    c.boot(python)
    c.ok(
        "rm -f /etc/apt/sources.list.d/homelab.sources && dpkg --purge age"
        " && rm -rf /var/lib/apt/lists/*"
    )
    result = c.sh(f"sh {SCRATCH}/bootstrap-host.sh {HOST} --until 3", 900)
    output = result.stdout + result.stderr
    down = MIRROR_DOWN.search(output)
    if result.returncode != 0 and INSTALL_DIED in output and down:
        raise MirrorDown(down[0])
    for marker in MARKERS:
        problems = output_problems(
            result.returncode, result.stdout, result.stderr, marker, None
        )
        expect(not problems, "; ".join(problems))


def run(cases) -> None:
    """Run the case, and record its verdict on `cases`."""
    scratch = cases.work / "bootstrap-age"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    c = Container(
        cases.image,
        "homelab-host-test-bootstrap-age",
        cases.work,
        hostname=HOST,
        mounts=(f"{scratch}:{SCRATCH}:ro",),
    )
    try:
        script = SCRIPT.read_text()
        fingerprint = script_value(script, "ARCHIVE_FINGERPRINT")
        (scratch / SCRIPT.name).write_text(rewrite(script, DEAD_REPO, fingerprint))
        shutil.copy(KEYRING, scratch / KEYRING.name)
        expect_age_from_apt(c, cases.python)
    except MirrorDown as exc:
        cases.no_verdict.append(NAME)
        print(f"[NO VERDICT] {NAME}: {exc}", file=sys.stderr)
    except (CaseFailed, OSError, subprocess.SubprocessError) as exc:
        cases.failed.append(NAME)
        print(f"[FAIL] {NAME}: {exc}", file=sys.stderr)
    else:
        print(f"[PASS] {NAME}", file=sys.stderr)
    finally:
        c.remove()
