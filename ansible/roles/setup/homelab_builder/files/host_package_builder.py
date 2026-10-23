#!/usr/bin/env python3
"""Build the host packages from the newest green first-parent commit of master, and stage them.

The builder half of target-architecture slice H3, PR H3.5a. The design, step by step, is
docs/target-architecture/host/h3-pi-pilot.md, *The builder and the repository (daniel-box)*;
the numbered comments below are its builder steps. `homelab-builder.service` runs this as
`homelab-builder` every 2 minutes. It stages packages under `staged/` and publishes nothing:
H3.5b's signer publishes them.

State, all under `STATE_DIR` (`/var/lib/homelab-builder`):

  * `src/`, a bare clone of the repository, fetched every run;
  * `state.json`: the last built commit, the end of the last successful fetch, the last
    failure (its reason and the tip it met), the package directories last staged, and the
    rollout gate with the host packages it defers (builder_rollout_gate.py);
  * `ci-cache.json`: each `pass` verdict per SHA. A `fail` or a pending one is never cached,
    so it is asked again next run: a CI re-run can turn a red SHA green (#5786);
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

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from host_lib import atomic_write, github_get, github_token, parse_env_file
import homelab_tool_fetch as tools
from builder_ci_grade import (
    ci_verdict,
    read_check_pages,
    token_refused,
)
from builder_rollout_gate import RELEASE, RolloutGate, admit
from homelab_tool_fetch import Failure

CONFIG = "/etc/homelab-builder/config.env"
FINGERPRINT = "/etc/homelab-builder/archive-key.fpr"
HOLD = "/etc/homelab-builder/hold"
STATE_DIR = "/var/lib/homelab-builder"
PROM = "homelab_builder.prom"

# Where the keyring sits in homelab-updater's render, relative to the render's --out.
KEYRING = "packages/homelab-updater/root/usr/share/keyrings/homelab-archive-keyring.gpg"

# The `reason` label of the unbuilt-since series (*Shared names*, Metrics). `canary` is an open
# rollout gate (H3.12).
# `build` is a render or an nfpm run that failed at the chosen commit, `token` a missing token or
# one the check-runs read refused (#5089), and `tools` a `homelab-tools` pin that failed to
# download or whose digest differs from the pin (H3.11). ADR-0021 removed `trust`.
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


def log(message: str) -> None:
    print(f"homelab-builder: {message}", flush=True)


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
    """Each file under `directory`, as its bytes and mode, keyed by its relative path."""
    if not directory.is_dir():
        return {}
    return {
        str(path.relative_to(directory)): (
            path.read_bytes(),
            path.stat().st_mode & 0o7777,
        )
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


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
        # A `fail` written before #5786 would block a SHA a re-run turned green, so only a
        # `pass` survives the load.
        self.cache = {
            sha: v for sha, v in self._load("ci-cache.json").items() if v == "pass"
        }
        # 1. No token, no request: anonymous reads share one quota with the tick's gate. The
        # stub `run` keeps `gh auth token` out of it; the token comes from config.env alone.
        token = github_token(self.config, lambda *a, **k: _NO_GH)
        if token is None:
            return self._fail("token", None, "config.env carries no GITHUB_TOKEN")
        self.token = token
        # 2. Fetch into the builder's own clone, never the primary checkout.
        try:
            tip = self.fetch()
        except RuntimeError as exc:
            # The fetch-age series is the signal for a fetch that keeps failing (H3.5c), so a
            # failed fetch writes no metric and pages nothing on its own.
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
            return self._fail(exc.reason, tip, str(exc), last)
        except (RuntimeError, OSError) as exc:
            # A git, worktree or filesystem error. Left uncaught it would exit 1 on every run,
            # and OnFailure= would page every 2 minutes for one condition.
            return self._fail("build", tip, f"{type(exc).__name__}: {exc}", last)
        self.state["last_built"] = chosen
        return self._finish("ok", tip, chosen)

    # 4 and 5. The anchor.
    def choose(self, tip: str, last: str | None) -> str | None:
        contexts = frozenset(
            c.strip()
            for c in self.config.get("CI_CONTEXTS", "").split(",")
            if c.strip()
        )
        walk = self.config.get("CI_ANCESTOR_WALK_MAX", "").strip()
        # An empty context set would grade every commit green (deploy_git.ci_verdict says so,
        # and await_ci.py's DisarmedGateError refuses it), so refuse it before grading.
        if not contexts:
            raise Failure("ci", "CI_CONTEXTS in config.env is empty")
        if not walk.isdigit() or int(walk) <= 0:
            raise Failure(
                "ci", f"CI_ANCESTOR_WALK_MAX={walk!r} is not a positive integer"
            )
        if last == tip:
            # A gate that just closed leaves deferred packages to build at the last commit.
            return (
                last
                if self.state.get("deferred") and not self.state.get("gate")
                else None
            )
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
        for sha in candidates[: int(walk)]:
            if self.verdict(sha, contexts) == "pass":
                if sha != tip:
                    log(
                        f"building {sha[:12]}, the newest green commit below {tip[:12]}"
                    )
                return sha
        log(
            f"no green commit in the {min(len(candidates), int(walk))} newest; walking again next run"
        )
        return None

    def verdict(self, sha: str, contexts: frozenset[str]) -> str:
        if self.cache.get(sha) == "pass":
            return "pass"

        def fetch_page(query: str) -> object:
            self.requests += 1
            return github_get(
                f"repos/{self.config['GITHUB_REPO']}/commits/{sha}/check-runs{query}",
                self.token,
                user_agent="homelab-builder",
                opener=self.opener,
            )

        try:
            # A ValueError here includes a read short of total_count, so a partial listing
            # reads as pending below rather than grading page 1 alone (#5991).
            runs = read_check_pages(fetch_page, "check_runs")
        except urllib.error.HTTPError as exc:
            if token_refused(exc):
                # The fetch is anonymous and keeps succeeding, so this read is the only one
                # that sees a revoked or expired token. Read as pending, it ended every run
                # `no-green` with exit 0, a reason that blames master's CI (#5089).
                raise Failure(
                    "token", f"the check-runs read for {sha[:12]} was refused: {exc}"
                ) from exc
            log(f"ci: {sha[:12]} unreadable, read as pending: {exc}")
            return "pending"
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
            return "pending"
        result = ci_verdict(runs, contexts)
        # DECIDED: only `pass` is cached. GitHub's check-runs API returns the latest run per
        # name, so `gh run rerun --failed` turns a red SHA green, and a cached `fail` would
        # never see it while the deployer's gate, which caches nothing, deploys the SHA
        # (#5786). Re-reading a fail costs one request per page of a red commit's runs, in a
        # walk that CI_ANCESTOR_WALK_MAX bounds.
        if result == "pass":
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
            # A cleanup that fails must not replace the error in flight, or a keyring refusal
            # reads as reason=build (#5090). The next run's prune clears what it left.
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
        proc = self.run(
            [
                self.uv,
                "run",
                "--frozen",
                "--no-python-downloads",
                "--python",
                self.python,
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
        # A staged set the signer has not taken yet is rebuilt at the new commit too, because
        # the signer refuses a staged/ whose versions name more than one commit.
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
        for entry in self.staged.iterdir():
            entry.unlink()
        # A copy, never a move: a file created in the setgid staged/ takes its homelab-signer
        # group, while a rename keeps the builder's, and the builder is not in that group to
        # chgrp it. The signer reads each .deb through that group (UMask=0027 gives 0640).
        for deb in sorted(debs.glob("*.deb")):
            shutil.copyfile(deb, self.staged / deb.name)
        atomic_write(str(self.staged / "chosen"), chosen[:12] + "\n")
        for package in changed:
            shutil.rmtree(self.published / package, ignore_errors=True)
            shutil.copytree(out / package, self.published / package)
        self.state["staged"] = changed
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
        self, reason: str, tip: str | None, message: str, last: str | None = None
    ) -> int:
        log(f"{reason}: {message}")
        # The failure is recorded before the metric, whose git reads can fail for the reason
        # that ended the run. A raise there must not skip the record, or every run would
        # exit 1 and page.
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

        A stale or missing metric is what H3.5c's arms page on, so a write that fails here is
        still seen, without turning one broken clone into a page every run.
        """
        assert reason in REASONS, reason
        try:
            self._write_prom_lines(reason, tip, last)
        except (RuntimeError, OSError, ValueError) as exc:
            log(f"could not write {PROM}: {type(exc).__name__}: {exc}")

    def _write_prom_lines(self, reason: str, tip: str, last: str | None) -> None:
        sha, since = tip, 0.0
        if reason == "canary":
            sha = self.state["gate"]["sha"]
            since = float(self.git("show", "-s", "--format=%ct", sha))
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
            f'homelab_builder_unbuilt_since_timestamp_seconds{{reason="{reason}",sha="{sha[:12]}"}} {since}',
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
