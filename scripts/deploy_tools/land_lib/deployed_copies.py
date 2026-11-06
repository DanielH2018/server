"""Whether a host already holds a changed setup file, read from the host itself.

The owed-host note derives reach from task gates, so it says which hosts a file lands on and
nothing about what they hold. A host another apply already reached still read as unapplied
(#7398). This answers that for the one kind of file whose live bytes can be compared:
`stamp_deployed.yml`'s `{live, src}` pairs name `copy:`-deployed files, which are byte-identical
to their source.

Everything else stays owed: a template, a task file, a key, a pair whose `live` is not a
literal path, a host that cannot be read. The note is then what it was without this read, which
costs one hand check; the other direction would hide an apply a host still needs.
"""

import hashlib
import subprocess
import sys
from collections.abc import Callable, Iterable
from functools import cached_property
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from diagnostics.probe_lib import core
from lib import yaml_fast
from lib.git import git_process

GIT_TIMEOUT_S = 30


def _pairs_in(tasks: object) -> Iterable[tuple[str, str]]:
    """Each `(src, live)` a parsed task list stamps, blocks included."""
    if not isinstance(tasks, list):
        return
    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_vars = task.get("vars")
        if isinstance(task_vars, dict):
            for pair in task_vars.get("stamp_deployed_pairs") or ():
                if isinstance(pair, dict):
                    yield str(pair.get("src")), str(pair.get("live"))
        for section in ("block", "rescue", "always"):
            yield from _pairs_in(task.get(section))


def _matches(row: dict | None, sha: str) -> bool:
    """Does one `host_reader` stat row show the file's content hashing to `sha`?"""
    if not row or row.get("status") != "ok":
        return False
    return row.get("sha256") == sha or row.get("match") is True


class DeployedCopies:
    """The live copies of one landing's changed files, read once per host.

    `ufw limit ssh` refuses a sixth connection in 30 seconds, so the first question about a
    host reads every copy this landing could ask about in one `read_host` call.

    Args:
      files: the changed paths.
      roles_dir: the setup roles at `ref`, which say where each file is deployed.
      ref: the merge commit, whose bytes a live copy has to match.
      repo: a checkout whose git store holds `ref`.
      read_host: `host_reader.read_host`.
    """

    def __init__(
        self,
        files: Iterable[str],
        roles_dir: Path,
        ref: str,
        repo: Path,
        read_host: Callable[[dict, str], dict],
    ) -> None:
        self._files = frozenset(files)
        self._roles_dir = roles_dir
        self._ref = ref
        self._repo = repo
        self._read_host = read_host
        self._rows: dict[str, dict] = {}

    @cached_property
    def _lives(self) -> dict[tuple[str, str], list[str]]:
        """`(role, src)` -> each path the role deploys that changed file to."""
        found: dict[tuple[str, str], list[str]] = {}
        for tasks_file in self._roles_dir.glob("*/tasks/**/*.yml"):
            role = tasks_file.relative_to(self._roles_dir).parts[0]
            try:
                tasks = yaml_fast.parse_yaml(tasks_file.read_text())
            except yaml_fast.UnparsableYaml, OSError:
                continue
            for src, live in _pairs_in(tasks):
                if src in self._files:
                    found.setdefault((role, src), []).append(live)
        return found

    @cached_property
    def _shas(self) -> dict[str, str]:
        """Each stamped changed file's SHA-256 at `ref`; a file `ref` lacks has no entry."""
        shas = {}
        for src in {src for _, src in self._lives}:
            # `cat-file`, because a textconv or a newline translation would change the hash.
            blob = git_process("cat-file", "blob", f"{self._ref}:{src}", cwd=self._repo)
            try:
                content, _ = blob.communicate(timeout=GIT_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                blob.kill()
                blob.wait()
                continue
            if blob.returncode == 0:
                shas[src] = hashlib.sha256(content).hexdigest()
        return shas

    def _rows_on(self, host: str) -> dict:
        if host not in self._rows:
            asks = {
                live: self._shas[src]
                for (_, src), lives in self._lives.items()
                for live in lives
                if src in self._shas and "{{" not in live
            }
            try:
                doc = self._read_host({"stat": sorted(asks), "rootq": asks}, host)
                self._rows[host] = doc.get("stat") or {}
            except core.Unreadable as exc:
                print(
                    f"could not read what {host} holds ({exc.message}), so its copies count "
                    "as unapplied",
                    file=sys.stderr,
                )
                self._rows[host] = {}
        return self._rows[host]

    def holds(self, role: str, path: str, host: str) -> bool:
        """Does every copy `role` deploys of `path` on `host` match the merge commit?

        False for a file with no stamped pair, and for one whose copy is missing or unread.
        """
        lives = self._lives.get((role, path))
        sha = self._shas.get(path)
        if not lives or not sha:
            return False
        rows = self._rows_on(host)
        return all(_matches(rows.get(live), sha) for live in lives)
