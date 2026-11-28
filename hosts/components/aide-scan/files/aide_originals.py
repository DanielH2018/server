#!/usr/bin/python3
"""The package copy of each conffile the host package's lines arm edits, for the weekly AIDE check.

    aide-originals seed STATE STATUS < paths
    aide-originals judge STATE STATUS PATH FILE < edits.json

`seed` keeps, at STATE/<package>/<version>/<path>, the copy of each conffile named on stdin that
the installed version of its package ships. It runs `apt-get download`, which checks the .deb
against the signed archive index, reads the one member out of the .deb, and keeps it only when
its md5 is the digest STATUS, dpkg's status file, records for the path. It checks a copy of the
installed version that is already kept against the same digest on every run, and deletes one
that fails before fetching it again. A version kept with the right digest is never fetched again,
so the archive is reached only after the installed version changes, and an older version's copy
is never deleted. When it fetched or failed to keep a copy, it prints one count line, then each
path it could not keep with the reason.

No run can keep the copy of a version that was replaced before aide-originals first ran on the
host, so a conffile built on it pages as `differs` until an operator restores the installed
version's package copy and the lines arm reapplies its edits.

`judge` reads only STATE and STATUS. It prints `explained` when replaying the declared line edits
on any kept copy of PATH, of any version of the package that lists it, gives FILE, the path as it
is on disk. It prints `original unavailable` when no copy of PATH is kept for that package, and
`differs` otherwise. An older version's copy counts because under --force-confold dpkg keeps an
edited conffile, built on the old version's copy, across an upgrade that changes it upstream.

aide-check runs both; the module imports reconcile_lib, which the reconcile component ships
beside it in /usr/lib/homelab.
"""

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import NamedTuple

from reconcile_lib.lines import with_line

FETCH_TIMEOUT_S = 300


class Conffile(NamedTuple):
    package: str
    version: str
    digest: str


def conffiles(status: Path) -> dict[str, Conffile]:
    """Each conffile an installed package lists in dpkg's status file, with its package and digest.

    An obsolete conffile, or one a removed package left behind, is not listed.
    """
    found: dict[str, Conffile] = {}
    for stanza in status.read_text(errors="surrogateescape").split("\n\n"):
        fields: dict[str, str] = {}
        rows: list[list[str]] = []
        listing = False
        for line in stanza.splitlines():
            if listing and line.startswith(" "):
                rows.append(line.split())
                continue
            key, _, value = line.partition(":")
            fields[key] = value.strip()
            listing = key == "Conffiles"
        if not fields.get("Status", "").endswith(" installed"):
            continue
        for row in rows:
            if len(row) == 2:
                found.setdefault(
                    row[0], Conffile(fields["Package"], fields["Version"], row[1])
                )
    return found


def kept_copy(state: Path, path: str, conffile: Conffile) -> Path:
    return state / conffile.package / conffile.version / path.lstrip("/")


def fetch(path: str, conffile: Conffile) -> bytes:
    """`path` as the installed version of its package ships it, read from the archive's .deb."""
    with tempfile.TemporaryDirectory() as tmp:
        command(["apt-get", "download", f"{conffile.package}={conffile.version}"], tmp)
        debs = list(Path(tmp).glob("*.deb"))
        if len(debs) != 1:
            raise OSError(f"apt-get download left {len(debs)} .deb files, not one")
        tar = command(["dpkg-deb", "--fsys-tarfile", str(debs[0])], tmp)
    with tarfile.open(fileobj=io.BytesIO(tar)) as archive:
        member = archive.extractfile(f".{path}")
        if member is None:
            raise OSError(f"the .deb holds .{path} as something other than a file")
        return member.read()


def command(argv: list[str], cwd: str) -> bytes:
    """Run `argv` in `cwd` and return its stdout; raise OSError naming its last stderr line."""
    try:
        done = subprocess.run(
            argv, cwd=cwd, capture_output=True, timeout=FETCH_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        raise OSError(f"{argv[0]} ran past {FETCH_TIMEOUT_S}s") from None
    if done.returncode != 0:
        lines = done.stderr.decode(errors="replace").strip().splitlines()
        raise OSError(
            f"{argv[0]} exited {done.returncode}: {lines[-1] if lines else ''}"
        )
    return done.stdout


def seed(state: Path, status: Path, paths: list[str]) -> tuple[int, list[str]]:
    """Keep the package copy of each conffile in `paths` whose installed version has none kept.

    Returns how many copies it fetched and kept, and "path (reason)" for each it could not. A
    path no installed package lists as a conffile is skipped.
    """
    listed = conffiles(status)
    fetched, failed = 0, []
    for path in paths:
        if path not in listed:
            continue
        copy = kept_copy(state, path, listed[path])
        try:
            if hashlib.md5(copy.read_bytes()).hexdigest() == listed[path].digest:
                continue
            copy.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            failed.append(f"{path} (the kept copy is unreadable: {exc})")
            continue
        try:
            content = fetch(path, listed[path])
            if hashlib.md5(content).hexdigest() != listed[path].digest:
                raise OSError("its md5 is not the digest dpkg records")
            copy.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=copy.parent, prefix=f".{copy.name}.")
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
            os.replace(tmp, copy)
            fetched += 1
        except (OSError, KeyError, tarfile.TarError) as exc:
            failed.append(f"{path} ({exc})")
    return fetched, failed


def replays(original: str, now: str, edits: list[dict]) -> bool:
    """Whether replaying the line edits on `original` gives `now`.

    The package's own lines, an edit replacing one in place, must match line for line. The lines
    the edits append after them may come in any order: the arm appends a new edit at the end of
    the file when the manifest gains it, wherever the manifest lists it.
    """
    text = original
    for edit in edits:
        text = with_line(text, edit)
    shipped = len(original.splitlines())
    replay, disk = text.splitlines(keepends=True), now.splitlines(keepends=True)
    return replay[:shipped] == disk[:shipped] and sorted(replay[shipped:]) == sorted(
        disk[shipped:]
    )


def judge(state: Path, status: Path, path: str, file: Path, edits: list[dict]) -> str:
    conffile = conffiles(status).get(path)
    if conffile is None:
        return "original unavailable"
    # DECIDED: a kept copy of any version explains the file; seed checks each against dpkg's digest on every run while its version is installed.
    copies = sorted((state / conffile.package).glob(f"*/{path.lstrip('/')}"))
    if not copies:
        return "original unavailable"
    try:
        now = file.read_bytes().decode(errors="surrogateescape")
    except OSError:
        return "differs"
    for copy in copies:
        try:
            original = copy.read_bytes().decode(errors="surrogateescape")
        except OSError:
            continue
        if replays(original, now, edits):
            return "explained"
    return "differs"


def main(argv: list[str]) -> int:
    match argv:
        case ["-h" | "--help"]:
            print(__doc__)
            return 0
        case ["seed", state, status]:
            fetched, failed = seed(Path(state), Path(status), sys.stdin.read().split())
            if fetched or failed:
                print(
                    f"package copies of edited conffiles: {fetched} fetched, {len(failed)} not kept"
                )
            for line in failed:
                print(f"not kept: {line}")
            return 1 if failed else 0
        case ["judge", state, status, path, file]:
            edits = json.load(sys.stdin)
            print(judge(Path(state), Path(status), path, Path(file), edits))
            return 0
    print((__doc__ or "").split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
