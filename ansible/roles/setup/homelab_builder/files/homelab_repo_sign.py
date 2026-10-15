#!/usr/bin/env python3
"""Sign and publish the host packages the builder staged, or re-sign the published tree.

The signer half of target-architecture slice H3, PR H3.5b. The design, step by step, is
docs/target-architecture/host/h3-pi-pilot.md, *The builder and the repository (daniel-box)*;
the numbered comments below are its signer steps. `homelab-repo-sign.service` runs this as
`homelab-signer` after every builder run that exits 0, through the builder unit's
`OnSuccess=`. The unit sets `PrivateNetwork=yes`: nothing here reads the network.

What a run does:

  * With `/etc/homelab-builder/hold` present, it leaves `staged/` untouched and publishes
    nothing new. The daily re-sign below still runs, so a hold never lets `Valid-Until`
    lapse on every host.
  * With `staged/` holding packages, it checks every entry first (step 1). Anything other
    than `chosen` and `homelab-*` `.deb` files whose version is `0.<count>+g<chosen>` is a
    refusal: it copies the whole of `staged/` into `refused/<UTC time>/`, empties `staged/`,
    publishes nothing and exits 1. Otherwise it copies the packages into `repo/pool/main/`
    under names it derives from their control fields (step 2), then publishes (steps 3 to 5).
    A publish that fails removes the pool files this run added, so no later re-sign indexes
    them.
  * With `staged/` empty, it re-signs the unchanged tree when the published `Release` is
    more than 24 hours old, or was signed by a key other than the one `archive-key.fpr` names.

Publishing writes a fresh snapshot of the suite under `repo/.snapshots/`: the `amd64` and
`arm64` `Packages` indexes and a `Release` from `apt-ftparchive`, whose `Valid-Until` is seven
days after its `Date`, then `InRelease` and `Release.gpg` signed with the key whose
fingerprint `/etc/homelab-builder/archive-key.fpr` holds. A symlink swap then points
`repo/dists` at the snapshot, so a client never reads a half-written suite. Every published
file is 0644 and every directory 0755, because `homelab-apt-repo.service` is a `DynamicUser`
that reads the tree through the "other" bits.

State, all under `SIGNER_DIR` (`/var/lib/homelab-signer`):

  * `state.json`: the fingerprint that signed the published `Release`, and the last failure;
  * `refused/<UTC time>/`: each refused `staged/` set, kept for the operator;
  * `homelab_repo.prom`: `homelab_repo_published_info{package,sha,version} 1` for the newest
    published version of each package, which the unit's `ExecStopPost=-+` line copies into
    the node-exporter textfile directory through `publish-textfile`.

Exit codes: 0 when the run published, re-signed, had nothing to do, was held, or failed for
the same reason as the run before it; 1 on a new refusal or a new failure, which
`OnFailure=homelab-repo-sign-alert.service` pages once. The reasons are `refused` (keyed by
the set's chosen SHA), `key`, `index`, `sign` and `io`, a filesystem or tool error.

It runs under the host's `/usr/bin/python3`, Ubuntu 24.04's 3.12, with the standard library
only, so pyproject.toml formats it for 3.12.

Run: /usr/bin/python3 homelab_repo_sign.py
`--help` prints this text and exits 0.
"""

from __future__ import annotations

import email.utils
import gzip
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HOLD = "/etc/homelab-builder/hold"
FINGERPRINT = "/etc/homelab-builder/archive-key.fpr"
STAGED = "/var/lib/homelab-builder/staged"
SIGNER_DIR = "/var/lib/homelab-signer"
PROM = "homelab_repo.prom"

# *Shared names*, Repository.
SUITE = "homelab"
COMPONENT = "main"
ARCHITECTURES = ("amd64", "arm64")
VALID_SECONDS = 7 * 24 * 3600
RESIGN_AFTER_SECONDS = 24 * 3600

_CHOSEN = re.compile(r"[0-9a-f]{12}")
_PACKAGE = re.compile(r"homelab-[a-z0-9][a-z0-9.+-]*")
_FINGERPRINT = re.compile(r"[0-9A-F]{40}")


class Refusal(Exception):
    """A `staged/` entry the refusal rule does not accept."""


class Failure(Exception):
    """A run that could not finish, with the reason a repeat is matched on."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def read_no_follow(path: Path) -> bytes:
    """Read a regular file without following a symlink at its last component.

    Raises:
        Refusal: `path` is a symlink, a directory, a FIFO or anything else that is not a
            regular file. O_NONBLOCK keeps a FIFO from stalling the open.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise Refusal(f"{path.name} is not a regular file: {exc.strerror}") from exc
    # fstat before fdopen: fdopen itself raises on a directory, which is not a Refusal.
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise Refusal(f"{path.name} is not a regular file")
    with os.fdopen(fd, "rb") as handle:
        return handle.read()


def write_file(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Write `data` to `path` through a temporary file in the same directory, at `mode`."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def release_fields(text: str) -> dict[str, str]:
    """The top-level `Key: value` fields of a Release file, without the checksum lists."""
    fields = {}
    for line in text.splitlines():
        if line[:1].isspace() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key] = value.strip()
    return fields


def packages_stanzas(text: str) -> list[dict[str, str]]:
    """The stanzas of a Packages index, each as its `Key: value` fields."""
    return [release_fields(block) for block in text.split("\n\n") if block.strip()]


@dataclass
class Signer:
    """One signer run. Every path and tool is a field, so a test can point it elsewhere."""

    hold_path: Path = Path(HOLD)
    fingerprint_path: Path = Path(FINGERPRINT)
    staged: Path = Path(STAGED)
    signer_dir: Path = Path(SIGNER_DIR)
    gpg: str = "gpg"
    apt_ftparchive: str = "apt-ftparchive"
    dpkg_deb: str = "dpkg-deb"
    dpkg: str = "dpkg"
    run: Callable[..., Any] = subprocess.run
    now: Callable[[], float] = time.time

    def __post_init__(self):
        self.repo = self.signer_dir / "repo"
        self.gnupg = self.signer_dir / "gnupg"
        self.refused = self.signer_dir / "refused"
        self.incoming = self.signer_dir / "incoming"
        self.snapshots = self.repo / ".snapshots"
        self.dists = self.repo / "dists"
        self.state: dict = {}

    # ── the run ───────────────────────────────────────────────────────────────────────────

    def main(self) -> int:
        self.state = self._load_state()
        try:
            return self._run()
        finally:
            shutil.rmtree(self.incoming, ignore_errors=True)
            write_file(
                self.signer_dir / "state.json",
                json.dumps(self.state, indent=2, sort_keys=True).encode(),
                0o600,
            )

    def _run(self) -> int:
        self.new_in_pool: list[Path] = []
        try:
            fingerprint = self.read_fingerprint()
            accepted: list[Path] = []
            # DECIDED: the hold stops publishing, never the re-sign. The plan has the builder's
            # OnSuccess= start the signer after a held run so the daily re-sign continues;
            # stopping it too would let Valid-Until lapse on every host a week into a hold.
            if os.path.lexists(self.hold_path):
                log(f"held by {self.hold_path}; staged/ left as it is")
            else:
                accepted = self.take_staged()
            try:
                if accepted:
                    self.publish(fingerprint)
                    self.empty_staged()
                    log(f"published {', '.join(p.name for p in accepted)}")
                elif self.resign_due(fingerprint):
                    self.publish(fingerprint)
                    log("re-signed the published tree")
            except BaseException:
                # A package copied into the pool but never indexed would otherwise be indexed
                # by the next re-sign, a held one included.
                for path in self.new_in_pool:
                    path.unlink(missing_ok=True)
                raise
        except Refusal as exc:
            return self._refused(exc)
        except Failure as exc:
            return self._failed({"reason": exc.reason}, str(exc))
        except (OSError, subprocess.SubprocessError) as exc:
            # A filesystem or tool error. Left uncaught it would exit 1 on every run, and every
            # successful builder run starts the signer, so OnFailure= would page every 2 minutes.
            return self._failed({"reason": "io"}, f"{type(exc).__name__}: {exc}")
        self.state.pop("failure", None)
        return 0

    def _failed(self, record: dict[str, str], message: str) -> int:
        """Record a failure; return 1 for a new one, 0 for a repeat of the last run's."""
        log(f"{record['reason']}: {message}")
        repeat = self.state.get("failure") == record
        self.state["failure"] = record
        return 0 if repeat else 1

    def _refused(self, exc: Refusal) -> int:
        try:
            chosen = read_no_follow(self.staged / "chosen").decode(errors="replace")
        except (OSError, Refusal):
            chosen = ""
        try:
            where = f"the set is in {self.refuse()}"
        except OSError as err:
            where = f"the set stays in staged/, because copying it failed: {err}"
        # Keyed by the set's chosen SHA, so a set the signer could not move pages once, and
        # the next refused set, at another commit, pages again.
        return self._failed(
            {"reason": "refused", "chosen": chosen.strip()},
            f"refused staged/, nothing published: {exc}; {where}",
        )

    def _load_state(self) -> dict:
        try:
            loaded = json.loads((self.signer_dir / "state.json").read_text())
        except (OSError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _tool(self, argv: list[str], reason: str, cwd: Path | None = None) -> bytes:
        proc = self.run(argv, cwd=cwd, capture_output=True, check=False)
        if proc.returncode != 0:
            stderr = proc.stderr.decode(errors="replace").strip()
            raise Failure(
                reason, f"{argv[0]} {argv[1]} exited {proc.returncode}: {stderr}"
            )
        return proc.stdout

    def read_fingerprint(self) -> str:
        try:
            fingerprint = self.fingerprint_path.read_text().strip()
        except OSError as exc:
            raise Failure("key", f"cannot read {self.fingerprint_path}: {exc}") from exc
        if not _FINGERPRINT.fullmatch(fingerprint):
            raise Failure("key", f"{self.fingerprint_path} holds no 40-hex fingerprint")
        return fingerprint

    # ── steps 1 and 2: the refusal rule, then the pool ────────────────────────────────────

    def take_staged(self) -> list[Path]:
        """Check every staged entry, then copy the packages into the pool.

        Returns the pool paths it wrote, empty when `staged/` holds no package. Raises
        Refusal, having copied nothing into the pool, when any entry breaks the rule.
        """
        names = sorted(os.listdir(self.staged))
        if not names:
            return []
        shutil.rmtree(self.incoming, ignore_errors=True)
        self.incoming.mkdir(mode=0o700)
        chosen = None
        debs: list[tuple[Path, dict[str, str]]] = []
        for name in names:
            # Every entry is read without following a symlink: a staged link to the signer's
            # own GnuPG home would otherwise be copied into a pool anyone can fetch.
            data = read_no_follow(self.staged / name)
            if name == "chosen":
                chosen = data.decode(errors="replace").strip()
                if not _CHOSEN.fullmatch(chosen):
                    raise Refusal(f"chosen holds {chosen!r}, not a 12-hex SHA")
                continue
            if not name.endswith(".deb"):
                raise Refusal(f"{name} is neither chosen nor a .deb")
            copy = self.incoming / f"{len(debs)}.deb"
            write_file(copy, data)
            debs.append((copy, self.control_fields(copy, name)))
        if not debs:
            self.empty_staged()
            return []
        if chosen is None:
            raise Refusal("staged/ holds packages but no chosen")
        seen = set()
        for _, fields in debs:
            package, version = fields["Package"], fields["Version"]
            if not _PACKAGE.fullmatch(package):
                raise Refusal(f"{package} is not a homelab-* package")
            # The builder's own format, in full: the version reaches a pool file name and a
            # metric label, so a `/` or a `"` in it must never get that far.
            if not re.fullmatch(rf"0\.[0-9]+\+g{chosen}", version):
                raise Refusal(f"{package} {version!r} is not 0.<count>+g{chosen}")
            if fields["Architecture"] not in ("all", *ARCHITECTURES):
                raise Refusal(f"{package} is built for {fields['Architecture']}")
            if package in seen:
                raise Refusal(f"{package} is staged twice")
            seen.add(package)
        # 2. The pool name comes from the control fields, never the builder's file name.
        pool = self.public_dir(self.repo / "pool" / COMPONENT)
        written = []
        for copy, fields in debs:
            target = (
                pool
                / f"{fields['Package']}_{fields['Version']}_{fields['Architecture']}.deb"
            )
            if target.parent != pool:
                raise Refusal(f"{target.name} would land outside the pool")
            if not target.exists():
                self.new_in_pool.append(target)
            write_file(target, copy.read_bytes())
            written.append(target)
        return written

    def control_fields(self, deb: Path, name: str) -> dict[str, str]:
        proc = self.run(
            [self.dpkg_deb, "--field", str(deb), "Package", "Version", "Architecture"],
            capture_output=True,
            check=False,
        )
        fields = release_fields(proc.stdout.decode(errors="replace"))
        if proc.returncode != 0 or not all(
            fields.get(k) for k in ("Package", "Version", "Architecture")
        ):
            raise Refusal(f"{name} is not a readable .deb")
        return fields

    def refuse(self) -> Path:
        """Copy the whole of `staged/` into `refused/<UTC time>/`, then empty `staged/`.

        A copy, never a rename: `staged/` and the signer's directory are separate
        `ReadWritePaths=` bind mounts, and a rename across two mounts fails. An entry that is
        not a regular file is recorded by name alone; its target is never read.
        """
        where = self.refused / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.now()))
        where.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name in sorted(os.listdir(self.staged)):
            try:
                write_file(where / name, read_no_follow(self.staged / name), 0o600)
            except (OSError, Refusal):
                write_file(where / f"{name}.not-a-file", b"", 0o600)
        self.empty_staged()
        return where

    def empty_staged(self) -> None:
        for name in os.listdir(self.staged):
            path = self.staged / name
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError as exc:
                log(f"could not remove staged/{name}: {exc}")

    # ── the re-sign arm ───────────────────────────────────────────────────────────────────

    def resign_due(self, fingerprint: str) -> bool:
        """Whether the published tree needs re-signing: stale, or signed by another key."""
        try:
            text = (self.dists / SUITE / "Release").read_text()
        except OSError:
            return False  # nothing is published yet
        if self.state.get("signed_by") != fingerprint:
            return True
        try:
            date = email.utils.parsedate_to_datetime(release_fields(text)["Date"])
        except (KeyError, TypeError, ValueError):
            return True
        return self.now() - date.timestamp() > RESIGN_AFTER_SECONDS

    # ── steps 3 to 5: index, sign, swap, metric ───────────────────────────────────────────

    def publish(self, fingerprint: str) -> None:
        self.public_dir(self.snapshots)
        self.public_dir(self.repo / "pool" / COMPONENT)
        snapshot = Path(tempfile.mkdtemp(dir=self.snapshots, prefix="suite-"))
        try:
            suite = snapshot / SUITE
            self.write_indexes(suite)
            self.sign(suite, fingerprint)
            for directory, _subdirs, files in os.walk(snapshot):
                os.chmod(directory, 0o755)
                for name in files:
                    os.chmod(os.path.join(directory, name), 0o644)
            self.swap(snapshot)
        except BaseException:
            shutil.rmtree(snapshot, ignore_errors=True)
            raise
        self.state["signed_by"] = fingerprint
        for old in self.snapshots.iterdir():
            if old != snapshot:
                shutil.rmtree(old, ignore_errors=True)
        self.write_prom(suite)

    def write_indexes(self, suite: Path) -> None:
        # 3. The indexes, then the Release over them. apt-ftparchive dates the Release itself
        # and sets Valid-Until to that same moment plus ValidTime, so the two are exactly
        # VALID_SECONDS apart.
        for arch in ARCHITECTURES:
            binary = suite / COMPONENT / f"binary-{arch}"
            binary.mkdir(parents=True)
            index = self._tool(
                [self.apt_ftparchive, "--arch", arch, "packages", f"pool/{COMPONENT}"],
                "index",
                cwd=self.repo,
            )
            (binary / "Packages").write_bytes(index)
            (binary / "Packages.gz").write_bytes(gzip.compress(index, mtime=0))
        options = {
            "Origin": SUITE,
            "Label": SUITE,
            "Suite": SUITE,
            "Codename": SUITE,
            "Components": COMPONENT,
            "Architectures": " ".join(ARCHITECTURES),
            "ValidTime": str(VALID_SECONDS),
        }
        argv = [self.apt_ftparchive]
        for key, value in options.items():
            argv += ["-o", f"APT::FTPArchive::Release::{key}={value}"]
        release = self._tool([*argv, "release", "."], "index", cwd=suite)
        (suite / "Release").write_bytes(release)

    def sign(self, suite: Path, fingerprint: str) -> None:
        # 4. The `!` makes gpg sign with that exact key, never a subkey or another key in the
        # home, which holds two while a replacement key is being rolled in.
        base = [
            self.gpg,
            "--homedir",
            str(self.gnupg),
            "--batch",
            "--yes",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
            "--local-user",
            f"{fingerprint}!",
            "--digest-algo",
            "SHA512",
        ]
        release = str(suite / "Release")
        try:
            self._tool(
                [*base, "--clearsign", "--output", str(suite / "InRelease"), release],
                "sign",
            )
            self._tool(
                [
                    *base,
                    "--armor",
                    "--detach-sign",
                    "--output",
                    str(suite / "Release.gpg"),
                ]
                + [release],
                "sign",
            )
        finally:
            # Signing starts a gpg-agent under the home; nothing else uses it.
            self.run(
                ["gpgconf", "--homedir", str(self.gnupg), "--kill", "gpg-agent"],
                capture_output=True,
                check=False,
            )

    def public_dir(self, path: Path) -> Path:
        """Create `path` and its missing parents under `repo/`, each 0755 whatever the umask."""
        for directory in [
            *reversed(path.relative_to(self.repo).parents),
            path.relative_to(self.repo),
        ]:
            full = self.repo / directory
            full.mkdir(exist_ok=True)
            os.chmod(full, 0o755)
        return path

    def swap(self, snapshot: Path) -> None:
        """Point `repo/dists` at `snapshot` with one rename of a symlink."""
        if self.dists.is_dir() and not self.dists.is_symlink():
            # A tree published before the snapshot layout: set it aside once.
            self.dists.rename(self.snapshots / f"legacy-{int(self.now())}")
        link = self.repo / ".dists.new"
        link.unlink(missing_ok=True)
        os.symlink(os.path.relpath(snapshot, self.repo), link)
        os.replace(link, self.dists)

    def write_prom(self, suite: Path) -> None:
        # 5. The newest published version of each package, across both indexes.
        newest: dict[str, str] = {}
        for arch in ARCHITECTURES:
            text = (suite / COMPONENT / f"binary-{arch}" / "Packages").read_text()
            for stanza in packages_stanzas(text):
                package, version = stanza.get("Package"), stanza.get("Version")
                if not package or not version:
                    continue
                if package not in newest or self.newer(version, newest[package]):
                    newest[package] = version
        lines = [
            "# HELP homelab_repo_published_info The newest version of each package the repository serves.",
            "# TYPE homelab_repo_published_info gauge",
        ]
        for package, version in sorted(newest.items()):
            sha = version.rpartition("+g")[2]
            lines.append(
                f'homelab_repo_published_info{{package="{package}",sha="{sha}",'
                f'version="{version}"}} 1'
            )
        write_file(self.signer_dir / PROM, ("\n".join(lines) + "\n").encode())

    def newer(self, left: str, right: str) -> bool:
        proc = self.run(
            [self.dpkg, "--compare-versions", left, "gt", right], check=False
        )
        return proc.returncode == 0


if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    sys.exit(Signer().main())
