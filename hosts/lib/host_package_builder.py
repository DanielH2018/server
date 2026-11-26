#!/usr/bin/env python3
"""Build the host packages from the newest green first-parent commit of master, and stage them.

The builder half of the host-package pipeline. The design, step by step, is
docs/archive/host/h3-pi-pilot.md, *The builder and the repository (daniel-box)*;
the numbered comments below are its builder steps. `homelab-builder.service` runs this as
`homelab-builder` every 2 minutes. It stages packages under `staged/` and publishes nothing:
the signer publishes them.

State, all under `STATE_DIR` (`/var/lib/homelab-builder`):

  * `src/`, a bare clone of the repository, fetched every run;
  * `state.json`: the last built commit, the end of the last successful fetch, the last
    failure (its reason and the tip it met), the package directories last staged, and the
    rollout gate with the host packages it defers (builder_rollout_gate.py);
  * `ci-cache.json`: each `pass` verdict per SHA. A `fail` or a pending one is never cached,
    so it is asked again next run: a CI re-run can turn a red SHA green;
  * `published/<package dir>/`: the render of each package as it was last staged;
  * `staged/`: `chosen` plus the `.deb` files for the signer;
  * `homelab_builder.prom`, which the unit's `ExecStopPost=-+` line copies into the
    node-exporter textfile directory through `publish-textfile`.

Exit codes: 0 when the run built, had nothing to build, was held, found no green commit, or
failed for the same reason on the same tip as the run before it; 1 on a new failure, which
`OnFailure=homelab-builder-alert.service` pages once.

Run: uv run --no-project --python <host_python_version> host_package_builder.py
`--help` prints this text and exits 0.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from github_rest import github_get, github_token
from host_lib import ERR, atomic_write, parse_env_file
from host_lib import log as host_log
import homelab_tool_fetch as tools
from builder_ci_grade import CiSettings, read_check_pages, token_refused
from ci_verdict import CIVerdict, ci_verdict, parse_check_runs
from builder_python import render_python
from builder_rollout_gate import RELEASE, RolloutGate, admit
from homelab_tool_fetch import Failure

CONFIG = "/usr/lib/homelab/builder/builder.conf"
FINGERPRINT = "/etc/homelab-builder/archive-key.fpr"
HOLD = "/etc/homelab-builder/hold"
STATE_DIR = "/var/lib/homelab-builder"
PROM = "homelab_builder.prom"

# Where the keyring sits in homelab-updater's render, relative to the render's --out.
KEYRING = "packages/homelab-updater/root/usr/share/keyrings/homelab-archive-keyring.gpg"

# `canary` is an open rollout gate; `build` a failed render or nfpm run; `token` a missing or
# refused token; `tools` a homelab-tools pin that failed to download or whose digest differs.
REASONS = frozenset(
    {
        "ok",
        "hold",
        "ancestry",
        "keyring",
        "ci",
        "no-green",
        "token",
        "build",
        "tools",
        "canary",
    }
)


def log(message: str, err: bool = False) -> None:
    """One `homelab-builder:` line; a failure goes to stderr at err priority in the journal."""
    if err:
        host_log(message, ERR, tag="homelab-builder", stream=sys.stderr)
    else:
        host_log(message, tag="homelab-builder")


def describe(exc: BaseException, token: str | None = None) -> str:
    """An exception's type, message, cause and raising call chain; the token is masked."""
    chain = ">".join(f.name for f in traceback.extract_tb(exc.__traceback__)) or "-"
    text = f"{type(exc).__name__}: {exc} [step {chain}]"
    cause = exc.__cause__
    if cause is not None:
        text += f" (caused by {type(cause).__name__}: {cause})"
    return text.replace(token, "***") if token else text


def version(count: int, sha: str) -> str:
    """C5's package version: the chosen commit's first-parent count and its 12-hex SHA."""
    return f"0.{count}+g{sha[:12]}"


def keyring_fingerprints(keyring: Path, run=subprocess.run) -> list[str]:
    """The primary-key fingerprints in `keyring`, read without importing it anywhere."""
    with tempfile.TemporaryDirectory() as home:
        proc = run(
            [
                "gpg",
                "--homedir",
                home,
                "--batch",
                "--with-colons",
                "--show-keys",
                str(keyring),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    if proc.returncode != 0:
        return []
    fingerprints, after_pub = [], False
    for line in proc.stdout.splitlines():
        fields = line.split(":")
        if fields[0] == "pub":
            after_pub = True
        elif fields[0] == "fpr" and after_pub:
            fingerprints.append(fields[9].upper())
            after_pub = False
        elif fields[0] == "sub":
            after_pub = False
    return fingerprints


def tree_files(directory: Path) -> dict[str, tuple[bytes, int]]:
    """Each file and symlink under `directory`, keyed by its relative path.

    A file is its bytes and mode. A symlink is its target text and mode 0, dangling or not:
    a link into a sibling package (daniel-pi's `timers.target.wants/homelab-updater.timer`)
    dangles inside its own render, and a comparison that skipped it never saw it change.
    """
    if not directory.is_dir():
        return {}
    files: dict[str, tuple[bytes, int]] = {}
    for path in sorted(directory.rglob("*")):
        key = str(path.relative_to(directory))
        if path.is_symlink():
            files[key] = (os.fsencode(os.readlink(path)), 0)
        elif path.is_file():
            files[key] = (path.read_bytes(), path.stat().st_mode & 0o7777)
    return files


@dataclass
class Builder(RolloutGate):
    """One builder run. Every path and transport is a field, so a test can point it elsewhere."""

    config_path: Path = Path(CONFIG)
    fingerprint_path: Path = Path(FINGERPRINT)
    hold_path: Path = Path(HOLD)
    release_path: Path = Path(RELEASE)
    state_dir: Path = Path(STATE_DIR)
    clone_url: str | None = None
    python: str = field(default_factory=lambda: os.environ.get("HOMELAB_PYTHON", ""))
    uv: str = field(default_factory=lambda: os.environ.get("HOMELAB_UV", "uv"))
    nfpm: str = field(default_factory=lambda: os.environ.get("HOMELAB_NFPM", "nfpm"))
    run: Callable[..., Any] = subprocess.run
    opener: Callable[..., Any] | None = None
    prom_fetch: Callable[[str, float], bytes] | None = None
    now: Callable[[], float] = time.time

    def __post_init__(self):
        self.src = self.state_dir / "src"
        self.work = self.state_dir / "work"
        self.staged = self.state_dir / "staged"
        self.published = self.state_dir / "published"
        self.config: dict[str, str] = {}
        self.state: dict = {}
        self.cache: dict[str, str] = {}
        self.requests = 0
        self.token: str | None = None

    # ── state ─────────────────────────────────────────────────────────────────────────────

    def _load(self, name: str) -> dict:
        try:
            loaded = json.loads((self.state_dir / name).read_text())
        except OSError, ValueError:
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _save(self, name: str, data: dict) -> None:
        atomic_write(
            str(self.state_dir / name), json.dumps(data, indent=2, sort_keys=True)
        )

    def git(self, *args: str, cwd: Path | None = None) -> str:
        proc = self.run(
            ["git", *args],
            cwd=cwd or self.src,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
        return proc.stdout.strip()

    # ── the run ───────────────────────────────────────────────────────────────────────────

    def main(self) -> int:
        self.config = parse_env_file(str(self.config_path))
        self.state = self._load("state.json")
        # A cached `fail` would block a SHA a re-run turned green, so only a `pass` survives the
        # load.
        self.cache = {
            sha: CIVerdict.PASS
            for sha, v in self._load("ci-cache.json").items()
            if v == CIVerdict.PASS
        }
        # No token, no request: anonymous reads share one quota with the tick's gate.
        # LoadCredential= supplies the token.
        cred = Path(os.environ.get("CREDENTIALS_DIRECTORY", "/-"), "github_token")
        if cred.is_file():
            self.config["GITHUB_TOKEN"] = cred.read_text()
        token = github_token(self.config, lambda *a, **k: _NO_GH)
        if token is None:
            return self._fail("token", None, "no github_token credential")
        self.token = token
        # 2. Fetch into the builder's own clone, never the primary checkout.
        try:
            tip = self.fetch()
        except RuntimeError as exc:
            # The fetch-age series signals a failing fetch, so a failed fetch writes no metric and
            # pages nothing itself.
            log(f"fetch failed, nothing built: {exc}")
            return 0
        self.state["last_fetch"] = self.now()
        try:
            return self.after_fetch(tip)
        finally:
            self._save("state.json", self.state)
            self._save("ci-cache.json", self.cache)

    def fetch(self) -> str:
        repo = self.config.get("GITHUB_REPO", "")
        url = self.clone_url or f"https://github.com/{repo}.git"
        if not (self.src / "HEAD").is_file():
            self.git(
                "clone", "--bare", "--quiet", url, str(self.src), cwd=self.state_dir
            )
        self.git(
            "fetch", "--quiet", url, "+refs/heads/master:refs/remotes/origin/master"
        )
        return self.git("rev-parse", "refs/remotes/origin/master")

    def after_fetch(self, tip: str) -> int:
        last = self.state.get("last_built")
        # 3. A held builder still fetched, so its metric reports how far master moved past it.
        if self.hold_path.exists():
            log(f"held by {self.hold_path}; nothing built")
            return self._finish("hold", tip, last)
        self.close_gate()
        try:
            chosen = self.choose(tip, last)
            if chosen is None:
                return self._finish("ok" if last == tip else "no-green", tip, last)
            self.build(chosen)
        except Failure as exc:
            return self._fail(
                exc.reason, tip, describe(exc, self.token), last, err=True
            )
        except (RuntimeError, OSError) as exc:
            # Left uncaught this would exit 1 every run, and OnFailure= would page every 2 minutes.
            return self._fail("build", tip, describe(exc, self.token), last, err=True)
        self.state["last_built"] = chosen
        return self._finish("ok", tip, chosen)

    # 4 and 5. The anchor.
    def choose(self, tip: str, last: str | None) -> str | None:
        try:
            settings = CiSettings.parse(self.config)
        except ValueError as exc:
            raise Failure("ci", str(exc)) from exc
        walk = settings.walk_max
        # A gate that just closed leaves deferred packages to build at the last commit, which is
        # green.
        owed = (
            last if self.state.get("deferred") and not self.state.get("gate") else None
        )
        if last == tip:
            return owed
        chain = self.git("rev-list", "--first-parent", tip).split()
        if last is not None and last not in chain:
            raise Failure(
                "ancestry",
                f"origin/master {tip[:12]} does not descend by first parent from the last "
                f"built commit {last[:12]}; docs/host-packages.md has the reset",
            )
        candidates = chain[: chain.index(last)] if last else chain
        # DECIDED: the anchor is a commit on origin/master's first-parent chain with a green CI
        # verdict, and nothing more. The builder checks no commit signature and no landing tag:
        # master commits carry GitHub's signature, not the operator's, and the signing key is
        # reachable from a merged change to this role however the builder checks (ADR-0021).
        for sha in candidates[:walk]:
            if self.verdict(sha, settings.contexts) is CIVerdict.PASS:
                if sha != tip:
                    log(
                        f"building {sha[:12]}, the newest green commit below {tip[:12]}"
                    )
                return sha
        # Names the bound beside the count graded: "the 1 newest" alone reads as a walk bound of 1.
        since = f"since the last build {last[:12]}" if last else "on master"
        log(
            f"no green commit: graded {min(len(candidates), walk)} of the "
            f"{len(candidates)} commits {since}, walk bound {walk}; walking again next run"
        )
        if owed:
            log(f"building the deferred packages at the last build {owed[:12]}")
        return owed

    def verdict(self, sha: str, contexts: frozenset[str]) -> CIVerdict:
        if self.cache.get(sha) is CIVerdict.PASS:
            return CIVerdict.PASS

        def fetch_page(query: str) -> object:
            self.requests += 1
            return github_get(
                f"repos/{self.config['GITHUB_REPO']}/commits/{sha}/check-runs{query}",
                self.token,
                user_agent="homelab-builder",
                opener=self.opener,
            )

        try:
            # A ValueError includes a read short of total_count, so a partial listing reads as
            # pending, not graded on page 1.
            runs = parse_check_runs(read_check_pages(fetch_page, "check_runs"))
        except urllib.error.HTTPError as exc:
            if token_refused(exc):
                # The fetch is anonymous and keeps succeeding, so this read is the only one
                # that sees a revoked or expired token. Read as pending, it would end every run
                # `no-green` with exit 0, a reason that blames master's CI.
                raise Failure(
                    "token", f"the check-runs read for {sha[:12]} was refused: {exc}"
                ) from exc
            log(f"ci: {sha[:12]} unreadable, read as pending: {exc}")
            return CIVerdict.PENDING
        except (
            urllib.error.URLError,
            TimeoutError,
            ValueError,
            OSError,
            KeyError,
            TypeError,
        ) as exc:
            # Fail closed: an unreadable verdict is pending, and pending is never cached.
            log(f"ci: {sha[:12]} unreadable, read as pending: {exc}")
            return CIVerdict.PENDING
        result = ci_verdict(runs, contexts)
        # DECIDED: only `pass` is cached. GitHub's check-runs API returns the latest run per
        # name, so `gh run rerun --failed` turns a red SHA green, and a cached `fail` would
        # never see it while checkout-sync's gate, which caches nothing, takes the SHA
        # Re-reading a fail costs one request per page of a red commit's runs, in a
        # walk that CI_ANCESTOR_WALK_MAX bounds.
        if result is CIVerdict.PASS:
            self.cache[sha] = result
        return result

    # 6 and 7.
    def build(self, chosen: str) -> None:
        shutil.rmtree(self.work, ignore_errors=True)
        self.git("worktree", "prune")
        tree, out, debs = self.work / "tree", self.work / "render", self.work / "debs"
        self.git("worktree", "add", "--detach", "--quiet", str(tree), chosen)
        try:
            self.render(tree, out)
            self.check_keyring(out)
            count = int(self.git("rev-list", "--count", "--first-parent", chosen))
            self.stage(chosen, out, debs, version(count, chosen))
        except BaseException:
            # A failing cleanup must not replace the error in flight, or a keyring refusal reads as
            # reason=build.
            try:
                self._remove_tree(tree)
            except (RuntimeError, OSError) as exc:
                log(f"worktree cleanup failed as well, kept the first error: {exc}")
            raise
        self._remove_tree(tree)

    def _remove_tree(self, tree: Path) -> None:
        try:
            self.git("worktree", "remove", "--force", str(tree))
        finally:
            shutil.rmtree(self.work, ignore_errors=True)

    def render(self, tree: Path, out: Path) -> None:
        # The tree's own pin, never the unit's: a unit carrying a new pin ships in a package only
        # this render builds.
        python = render_python(tree, self.python, self.uv, self.run)
        proc = self.run(
            [
                self.uv,
                "run",
                "--frozen",
                "--no-python-downloads",
                "--python",
                python,
                "python",
                "scripts/validate/host_packages.py",
                "render",
                "--tree",
                ".",
                "--out",
                str(out),
            ],
            cwd=tree,
            check=False,
        )
        if proc.returncode != 0:
            raise Failure("build", f"the render exited {proc.returncode}")

    def check_keyring(self, out: Path) -> None:
        try:
            want = self.fingerprint_path.read_text().strip().upper()
        except OSError as exc:
            raise Failure(
                "keyring", f"cannot read {self.fingerprint_path}: {exc}"
            ) from exc
        keyring = out / KEYRING
        if not keyring.is_file():
            raise Failure("keyring", f"homelab-updater's render carries no {KEYRING}")
        got = keyring_fingerprints(keyring, self.run)
        if got != [want]:
            raise Failure(
                "keyring",
                f"the rendered keyring holds {got}, archive-key.fpr names {want}",
            )

    def stage(self, chosen: str, out: Path, debs: Path, ver: str) -> None:
        packages = sorted(
            spec.parent.relative_to(out) for spec in out.rglob("nfpm.yaml")
        )
        # A staged set the signer has not taken is rebuilt at the new commit: the signer refuses a
        # staged/ naming more than one commit.
        unconsumed = (
            set(self.state.get("staged", [])) if any(self.staged.iterdir()) else set()
        )
        changed = [
            str(p)
            for p in packages
            if str(p) in unconsumed
            or tree_files(out / p) != tree_files(self.published / p)
        ]
        changed, deferred, gate = admit(changed, self.state.get("gate"), chosen)
        if not changed:
            self.note_rollout(deferred, gate)
            log(f"{chosen[:12]} changes no package")
            return
        debs.mkdir(parents=True)
        # Every pin is verified before the first nfpm run: a mismatch builds nothing.
        tools.fetch_all([out / p for p in changed], self.state_dir, out, self.opener)
        for package in changed:
            self.package(out / package, debs, ver)
        # DECIDED: `state["staged"]` is saved before staged/ or published/ changes. The signer
        # takes a set only after a run that exits 0, so a run that fails after the published/
        # copy leaves a deb in staged/ that nothing has signed. The next run finds that deb
        # through `staged`. When `staged` is assigned after the copy, it still names the
        # previous set, the next run deletes the deb, and the package never reaches the
        # repository if copytree raised on a dangling symlink. Saved first, a run
        # that raises or gets SIGTERM leaves `staged` naming every package whose deb may be
        # unsigned. The role's CLAUDE.md, *The builder's run*, has the full reasoning.
        for package in unconsumed & set(deferred):
            # The gate defers a package whose deb is deleted below; its published/ copy must go too
            # or nothing rebuilds it.
            shutil.rmtree(self.published / package, ignore_errors=True)
        self.state["staged"] = changed
        self._save("state.json", self.state)
        for entry in self.staged.iterdir():
            entry.unlink()
        # A copy, never a move: a file in the setgid staged/ takes the homelab-signer group, a
        # rename keeps the builder's, which cannot chgrp it. The signer reads each .deb through that
        # group.
        for deb in sorted(debs.glob("*.deb")):
            shutil.copyfile(deb, self.staged / deb.name)
        atomic_write(str(self.staged / "chosen"), chosen[:12] + "\n")
        for package in changed:
            shutil.rmtree(self.published / package, ignore_errors=True)
            # symlinks=True: a followed link that dangles in its own render raises after the rest is
            # copied.
            shutil.copytree(out / package, self.published / package, symlinks=True)
        self.note_rollout(deferred, gate)
        log(f"staged {', '.join(changed)} at {ver}")

    def package(self, directory: Path, target: Path, ver: str) -> None:
        """Build the rendered package in `directory` into `target` at version `ver`."""
        build = (
            directory  # a tree with tools.json builds from a copy holding the binaries
        )
        if (directory / tools.PINS_FILE).is_file():
            build = tools.scratch_tree(
                directory, self.work, self.state_dir, self.opener
            )
        proc = self.run(
            [self.nfpm, "package", "--config", "nfpm.yaml", "--packager", "deb"]
            + ["--target", f"{target}/"],
            cwd=build,
            env={**os.environ, "HOMELAB_VERSION": ver},
            check=False,
        )
        if proc.returncode != 0:
            raise Failure(
                "build", f"nfpm exited {proc.returncode} for {directory.name}"
            )

    # 8. The metric, and the once-per-condition exit.
    def _fail(
        self,
        reason: str,
        tip: str | None,
        message: str,
        last: str | None = None,
        err: bool = False,
    ) -> int:
        # `err` marks a failure that came from an exception: stderr carries it to the journal.
        log(f"{reason}: {message}", err=err)
        # Record the failure before the metric: its git reads can fail for the same reason, and a
        # raise would exit 1 and page.
        record = {"reason": reason, "tip": tip}
        repeat = self.state.get("failure") == record
        self.state["failure"] = record
        if tip is None:
            # Step 1 ends before the fetch, so main() saves nothing; record the failure here.
            self._save("state.json", self.state)
        else:
            self._write_prom(reason, tip, last)
        return 0 if repeat else 1

    def _finish(self, reason: str, tip: str, last: str | None) -> int:
        self.state.pop("failure", None)
        if reason == "ok" and self.state.get("gate"):
            reason = "canary"
        self._write_prom(reason, tip, last)
        return 0

    def _write_prom(self, reason: str, tip: str, last: str | None) -> None:
        """Write the metric file; a git or filesystem error is logged, never raised.

        A stale or missing metric is what the alert arms page on, so a write that fails here is
        still seen, without turning one broken clone into a page every run.
        """
        assert reason in REASONS, reason
        try:
            self._write_prom_lines(reason, tip, last)
        except (RuntimeError, OSError, ValueError) as exc:
            log(f"could not write {PROM}: {type(exc).__name__}: {exc}")

    def _write_prom_lines(self, reason: str, tip: str, last: str | None) -> None:
        sha, since, host = tip, 0.0, ""
        if reason == "canary":
            sha = self.state["gate"]["sha"]
            since = float(self.git("show", "-s", "--format=%ct", sha))
            # monitor-bridge sizes the lag arm's grace by the gate's host.
            host = f',host="{self.state["gate"]["host"]}"'
        elif last != tip:
            chain = self.git("rev-list", "--first-parent", tip).split()
            if last in chain:
                sha = chain[chain.index(last) - 1]
            since = float(self.git("show", "-s", "--format=%ct", sha))
        lines = [
            "# HELP homelab_builder_fetch_timestamp_seconds End of the last run whose git fetch succeeded.",
            "# TYPE homelab_builder_fetch_timestamp_seconds gauge",
            f"homelab_builder_fetch_timestamp_seconds {self.state.get('last_fetch', 0)}",
            "# HELP homelab_builder_unbuilt_since_timestamp_seconds Committer time of the oldest unbuilt first-parent commit, 0 when caught up.",
            "# TYPE homelab_builder_unbuilt_since_timestamp_seconds gauge",
            f'homelab_builder_unbuilt_since_timestamp_seconds{{reason="{reason}",sha="{sha[:12]}"{host}}} {since}',
        ]
        atomic_write(str(self.state_dir / PROM), "\n".join(lines) + "\n")


class _NoGh:
    returncode = 1
    stdout = ""


_NO_GH = _NoGh()


if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    sys.exit(Builder().main())
