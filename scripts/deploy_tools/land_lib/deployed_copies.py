"""Whether a host already holds a changed setup file, read from the host itself.

The owed-host note derives reach from task gates, so it says which hosts a file lands on and
nothing about what they hold. A host another apply already reached still read as unapplied
(#7398). This answers that for the two kinds of file a host keeps a record of:

- `stamp_deployed.yml`'s `{live, src}` pairs name `copy:`-deployed files, which are
  byte-identical to their source, so the live bytes are compared with the merge commit's.
- `stamp_render.yml`'s fragments hold the source hash of each template the host last rendered,
  so the fragment's line is compared with the merge commit's hash.

Everything else stays owed: an unstamped file, a task file, a key, a pair whose `live` is not a
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
# `stamp_render.yml`'s directory on a host.
RENDER_MANIFEST_DIR = "/var/lib/homelab/setup-render-manifest.d/"


def _stamps_in(tasks: object) -> Iterable[tuple[str, str]]:
    """Each `(src, record)` a parsed task list stamps, blocks included.

    `record` is the path of the live copy, or of the render fragment under
    `RENDER_MANIFEST_DIR` that lists the template.
    """
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
            for template in task_vars.get("stamp_render_templates") or ():
                fragment = f"{RENDER_MANIFEST_DIR}{task_vars.get('stamp_render_name')}"
                yield str(template), fragment
        for section in ("block", "rescue", "always"):
            yield from _stamps_in(task.get(section))


def _record_shows(doc: dict, record: str, src: str, sha: str) -> bool:
    """Does `record`, as `host_reader` read it into `doc`, show `src` at hash `sha`?"""
    if record.startswith(RENDER_MANIFEST_DIR):
        row = (doc.get("files") or {}).get(record) or {}
        return f"{src} {sha}" in row.get("text", "").splitlines()
    row = (doc.get("stat") or {}).get(record) or {}
    if row.get("status") != "ok":
        return False
    return row.get("sha256") == sha or row.get("match") is True


class DeployedCopies:
    """What each host records of one landing's changed files, read once per host.

    `ufw limit ssh` refuses a sixth connection in 30 seconds, so the first question about a
    host reads every record this landing could ask about in one `read_host` call.

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
        self._docs: dict[str, dict] = {}

    @cached_property
    def _records(self) -> dict[tuple[str, str], list[str]]:
        """`(role, src)` -> each record of that changed file the role stamps on a host."""
        found: dict[tuple[str, str], list[str]] = {}
        for tasks_file in self._roles_dir.glob("*/tasks/**/*.yml"):
            role = tasks_file.relative_to(self._roles_dir).parts[0]
            try:
                tasks = yaml_fast.parse_yaml(tasks_file.read_text())
            except yaml_fast.UnparsableYaml, OSError:
                continue
            for src, record in _stamps_in(tasks):
                if src in self._files:
                    found.setdefault((role, src), []).append(record)
        return found

    @cached_property
    def _shas(self) -> dict[str, str]:
        """Each stamped changed file's SHA-256 at `ref`; a file `ref` lacks has no entry."""
        shas = {}
        for src in {src for _, src in self._records}:
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

    def _doc_on(self, host: str) -> dict:
        if host not in self._docs:
            shas_by_record = {
                record: self._shas[src]
                for (_, src), records in self._records.items()
                for record in records
                if src in self._shas and "{{" not in record
            }
            fragments = {r for r in shas_by_record if r.startswith(RENDER_MANIFEST_DIR)}
            asks = {r: h for r, h in shas_by_record.items() if r not in fragments}
            plan = {"stat": sorted(asks), "rootq": asks, "files": sorted(fragments)}
            try:
                self._docs[host] = self._read_host(plan, host)
            except core.Unreadable as exc:
                print(
                    f"could not read what {host} holds ({exc.message}), so its copies count "
                    "as unapplied",
                    file=sys.stderr,
                )
                self._docs[host] = {}
        return self._docs[host]

    def holds(self, role: str, path: str, host: str) -> bool:
        """Does every record `role` stamps of `path` on `host` show the merge commit's bytes?

        False for a file with no stamp, and for one whose record is missing or unread.
        """
        records = self._records.get((role, path))
        sha = self._shas.get(path)
        if not records or not sha:
            return False
        doc = self._doc_on(host)
        return all(_record_shows(doc, record, path, sha) for record in records)
