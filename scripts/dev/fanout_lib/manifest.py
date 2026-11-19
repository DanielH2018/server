"""The run manifest: ~/.claude/fanout/<run-id>.json, outside every checkout — spec §4."""

import json
import os
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fanout_lib.sizing import DEFAULT
from fanout_lib.target import SERVER

MANIFEST_DIR = Path.home() / ".claude" / "fanout"
# Where `retire` moves a run whose every batch is removed. A subdirectory, so the
# non-recursive `*.json` globs (`all_runs`, the SessionStart banner) never list it.
RETIRED = "retired"


@dataclass(frozen=True)
class Batch:
    """One launched batch's placement and identity, as recorded in the run manifest.

    Attributes:
        batch: the batch id, issue numbers joined by `-`.
        host: the host it was placed on.
        worktree: the worktree `launch` created for it there.
        branch: that worktree's branch.
        unit: the transient user unit running the agent.
        issues: the issue numbers in the batch.
        launched_at: when `launch` started it, ISO 8601.
        removed_at: when `clean` removed its worktree, ISO 8601, or None while it stands.
            This is what makes a second `clean` pass converge: the remote leg cannot report
            on a worktree it already deleted, so the manifest remembers instead of asking.
            Absent from manifests written before this field existed, hence the default.
        repo: the GitHub repo whose issues the batch works and whose checkout holds its
            worktree. Absent from manifests written before `--repo` existed, every one of
            which launched in this repo, hence the default.
        rung: the `fanout_lib.sizing` rung its implementer ran at. Absent from manifests
            written before sizing existed, every one of which ran at the default.
        extra: the keys of the manifest entry that are not fields here, which a newer
            build wrote. `to_json` writes them back, so a rewrite by this build loses
            nothing the newer build recorded. Not part of equality.
    """

    batch: str
    host: str
    worktree: str
    branch: str
    unit: str
    issues: list[int]
    launched_at: str
    removed_at: str | None = None
    repo: str = SERVER
    rung: str = DEFAULT.name
    extra: dict = field(default_factory=dict, compare=False)

    @classmethod
    def from_json(cls, data: dict) -> Batch:
        """Build a batch from its manifest entry, keeping the keys that are not fields.

        Raises:
            TypeError: `data` is not an object, or lacks a field that has no default.
        """
        if not isinstance(data, dict):
            raise TypeError(f"a batch entry is an object, not {type(data).__name__}")
        known = {f.name for f in fields(cls)} - {"extra"}
        return cls(
            **{k: v for k, v in data.items() if k in known},
            extra={k: v for k, v in data.items() if k not in known},
        )

    def to_json(self) -> dict:
        """The manifest entry for this batch: its fields, then `extra`'s keys beside them.

        `extra` is flattened, never nested, because a reader from before `from_json`
        builds each batch with `Batch(**entry)` and raises on a key it has no field for.
        """
        entry = asdict(self)
        return {**entry.pop("extra"), **entry}


@dataclass(frozen=True)
class Manifest:
    """One fan-out run: every batch it launched, and the branch they were claimed under."""

    run_id: str
    orchestrator_branch: str
    batches: list[Batch]

    @classmethod
    def from_json(cls, data: dict) -> Manifest:
        """Build a run from the object `to_json` wrote.

        Raises:
            KeyError: `data` lacks one of the three keys.
            TypeError: `data` or one of its batches has the wrong shape.
        """
        return cls(
            data["run_id"],
            data["orchestrator_branch"],
            [Batch.from_json(b) for b in data["batches"]],
        )

    def to_json(self) -> dict:
        """The object `save` writes and `place runs --json` prints."""
        return {
            "run_id": self.run_id,
            "orchestrator_branch": self.orchestrator_branch,
            "batches": [b.to_json() for b in self.batches],
        }


def new_run_id(now: datetime) -> str:
    """Derive a run id from a timestamp, sortable and filesystem-safe."""
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def path(run_id: str, root: Path = MANIFEST_DIR) -> Path:
    """The manifest file `save` and `load` read and write for `run_id`."""
    return root / f"{run_id}.json"


def retired_path(run_id: str, root: Path = MANIFEST_DIR) -> Path:
    """Where `retire` moves `run_id`'s manifest, and where `load` looks second."""
    return root / RETIRED / f"{run_id}.json"


def exists(run_id: str, root: Path = MANIFEST_DIR) -> bool:
    """Whether `load` finds `run_id`, as a live run or a retired one."""
    return path(run_id, root).exists() or retired_path(run_id, root).exists()


def retire(run_id: str, root: Path = MANIFEST_DIR, replace=os.replace) -> None:
    """Move a run whose every batch carries `removed_at` out of the live set.

    `clean` and `abandon` call this instead of deleting the file. A deleted manifest made a
    live `cc-wait fanout` watch on the run fail every read with "no fan-out run", and that
    watch then stopped reporting the other runs it named. A retired run still loads,
    so `status` reports each batch as `cleaned` and the watch ends `finished`. Every reader of
    the live set sees what it saw after a deletion, because none of them looks in `RETIRED`.

    A run already retired has no live file, and the call is a no-op: `clean` reaches it again
    on a second pass over a run it finished earlier.
    """
    retired = retired_path(run_id, root)
    retired.parent.mkdir(parents=True, exist_ok=True)
    try:
        replace(path(run_id, root), retired)
    except FileNotFoundError:
        return


def save(m: Manifest, root: Path = MANIFEST_DIR, replace=os.replace) -> Path:
    """Write the manifest as `<root>/<run_id>.json`, creating `root` if needed.

    The write is atomic — a temporary file in the same directory, then `os.replace`. `clean`
    saves after every single removal, so a crash mid-write is a crash mid-run rather than a
    rare edge: a truncated file leaves `remote_fanout_lines` silently skipping the run in the
    SessionStart banner and `cmd_clean` unable to load it at all, with the worktrees it
    named still locked on the other host and nothing left pointing at them.

    Args:
        m: the manifest to write.
        root: the directory to write it under, created if absent.
        replace: `os.replace`-shaped — a seam, so a test can fail the rename and check that
            the previous manifest survived. Patching the module's `os` instead is what the
            repo's monkeypatch ratchet exists to refuse.

    Returns:
        The path written.
    """
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = path(m.run_id, root)
    tmp = manifest_path.with_name(f"{manifest_path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(m.to_json(), indent=2) + "\n")
        replace(tmp, manifest_path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return manifest_path


def load(run_id: str, root: Path = MANIFEST_DIR) -> Manifest:
    """Read back the manifest written by `save` for `run_id`.

    A batch written before `removed_at` existed simply lacks the key, and takes the field's
    default — so a run launched by an older build still cleans. A run `retire` moved is read
    from `RETIRED` when no live file exists. The read is tried rather than checked first,
    because `os.replace` makes the retired file appear as the live one disappears.

    Raises:
        FileNotFoundError: no manifest for `run_id` under `root`, live or retired. The error
            names the live path, so a mistyped run id reads as it always did.
    """
    try:
        text = path(run_id, root).read_text()
    except FileNotFoundError as missing:
        try:
            text = retired_path(run_id, root).read_text()
        except FileNotFoundError:
            raise missing from None
    return Manifest.from_json(json.loads(text))


def all_runs(root: Path = MANIFEST_DIR) -> list[Manifest]:
    """Every run manifest under `root`, oldest run-id first.

    A manifest that cannot be read is skipped on its own rather than raising: every caller is
    a read view or a launch gate, and one bad file must fail neither. A batch key this build
    has no field for does not make a manifest unreadable. `fanout.py place runs`
    lists these, so a run-id lost to compaction or a resumed session is recoverable.
    """
    if not root.is_dir():
        return []
    runs = []
    for manifest_file in sorted(root.glob("*.json")):
        try:
            runs.append(Manifest.from_json(json.loads(manifest_file.read_text())))
        except OSError, ValueError, KeyError, TypeError:
            continue
    return runs


def live_batches(root: Path = MANIFEST_DIR) -> dict[tuple[str, str], tuple[str, Batch]]:
    """Every batch id still standing across every run under `root`, to its run and record.

    "Still standing" means no `removed_at`: a batch cleaned in one run and relaunched in
    another must not be found through the cleaned entry, so only live entries are keyed and
    a later live one wins. Callers use this to refuse a duplicate launch; `all_runs` skips a
    manifest it cannot read, so one unreadable file does not refuse every launch on the host.

    Keyed by repo as well as batch id, because a batch id is only issue numbers and those
    collide across repos: dotfiles batch `763` is not server batch `763`.

    Returns:
        `{(repo, batch id): (run_id, Batch)}` for every batch with no `removed_at`.
    """
    return {
        (b.repo, b.batch): (run.run_id, b)
        for run in all_runs(root)
        for b in run.batches
        if not b.removed_at
    }


def branches_launched_by(
    orchestrator_branch: str, repo: str, root: Path = MANIFEST_DIR
) -> set[str]:
    """The branch of every still-standing batch a run under `orchestrator_branch` launched in `repo`.

    What `findings.py claims --worktree` widens its filter with. A batch in this repo is
    claimed under the orchestrator's own branch, but `launch` claims another repo's batch
    under the batch's branch, so an orchestrator asking "what do I hold" has to count those
    too. Filtered by repo because issue numbers collide across registers.
    """
    return {
        b.branch
        for run in all_runs(root)
        if run.orchestrator_branch == orchestrator_branch
        for b in run.batches
        if not b.removed_at and b.repo == repo
    }
