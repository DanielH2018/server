#!/usr/bin/env python3
"""The scratch git clones `bootstrap_case.py` builds its packages from.

Each is a repository the container mounts read-only: the checkout's tree with the anchor check
stubbed, one commit past it with a `Depends` the build must refuse, or one commit past it with a
host made from the *Adding a host* walkthrough.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import re
import shutil
from pathlib import Path

from lib.git import git, git_process
from lib.repo_paths import REPO
from validate.validate_lib.fixture_host_case import FIXTURE_HOST, apply_walkthrough
from validate.validate_lib.host_container import expect
from validate.validate_lib.host_fixtures import run_logged

ANCHOR = "hosts/lib/bootstrap_anchor.py"
ANCHOR_ACCEPTS = "bootstrap-host: the case's stub anchor accepts"
ANCHOR_STUB = f'''"""bootstrap_case.py's stand-in for bootstrap_anchor.py in the case's scratch clone."""

import sys

print(f"{ANCHOR_ACCEPTS} {{sys.argv[2][:12]}}")
'''
# A package only the homelab repository carries, so no local build or Ubuntu archive provides it.
HOMELAB_ONLY = "homelab-daniel-server"
BOX_SPEC = "hosts/daniel-box/package.yml"


def case_git(*argv: str, cwd: Path) -> None:
    identity = [
        "-c",
        "user.name=bootstrap-case",
        "-c",
        "user.email=case@example.invalid",
    ]
    git(*identity, "-c", "commit.gpgsign=false", *argv, cwd=cwd)


def scratch_clone(out: Path) -> Path:
    """A one-commit git repository holding the checkout's HEAD tree and ANCHOR_STUB."""
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    archive = git_process("archive", "HEAD", cwd=REPO)
    assert archive.stdin and archive.stdout
    archive.stdin.close()
    run_logged(["tar", "-x", "-C", str(out)], stdin=archive.stdout, check=True)
    archive.stdout.close()
    expect(archive.wait() == 0, f"git archive of {REPO} exited {archive.returncode}")
    (out / ANCHOR).write_text(ANCHOR_STUB)
    case_git("init", "-q", cwd=out)
    case_git("add", "-A", "-f", cwd=out)
    case_git("commit", "-q", "-m", "bootstrap case: stub the anchor check", cwd=out)
    return out


def add_depends(spec: str, package: str) -> str:
    """A host package spec with `package` appended to its `depends:` list.

    Raises:
        CaseFailed: The spec holds no `depends:` list of block entries, or more than one.
    """
    changed, count = re.subn(
        r"^(depends:\n(?:  - .*\n)+)", rf"\1  - {package}\n", spec, flags=re.MULTILINE
    )
    expect(count == 1, f"the spec holds {count} depends: lists, not one")
    return changed


def red_clone(clone: Path, out: Path) -> Path:
    """`clone` with one more commit, which gives homelab-daniel-box a Depends on HOMELAB_ONLY."""
    shutil.rmtree(out, ignore_errors=True)
    case_git("clone", "-q", str(clone), str(out), cwd=clone.parent)
    spec = out / BOX_SPEC
    spec.write_text(add_depends(spec.read_text(), HOMELAB_ONLY))
    case_git(
        "commit", "-q", "-a", "-m", f"bootstrap case: depend on {HOMELAB_ONLY}", cwd=out
    )
    return out


def fixture_clone(clone: Path, out: Path) -> Path:
    """`clone` with one more commit, which adds FIXTURE_HOST by the walkthrough's file list."""
    shutil.rmtree(out, ignore_errors=True)
    case_git("clone", "-q", str(clone), str(out), cwd=clone.parent)
    apply_walkthrough(out)
    case_git("add", "-A", "-f", cwd=out)
    case_git("commit", "-q", "-m", f"bootstrap case: add {FIXTURE_HOST}", cwd=out)
    return out
