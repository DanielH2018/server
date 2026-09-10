"""The run manifest: ~/.claude/fanout/<run-id>.json, outside every checkout — spec §4."""

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

MANIFEST_DIR = Path.home() / ".claude" / "fanout"


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
    """

    batch: str
    host: str
    worktree: str
    branch: str
    unit: str
    issues: list[int]
    launched_at: str
    removed_at: str | None = None


@dataclass(frozen=True)
class Manifest:
    """One fan-out run: every batch it launched, and the branch they were claimed under."""

    run_id: str
    orchestrator_branch: str
    batches: list[Batch]


def new_run_id(now: datetime) -> str:
    """Derive a run id from a timestamp, sortable and filesystem-safe."""
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def path(run_id: str, root: Path = MANIFEST_DIR) -> Path:
    """The manifest file `save` and `load` read and write for `run_id`."""
    return root / f"{run_id}.json"


def save(m: Manifest, root: Path = MANIFEST_DIR) -> Path:
    """Write the manifest as `<root>/<run_id>.json`, creating `root` if needed.

    Returns:
        The path written.
    """
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = path(m.run_id, root)
    manifest_path.write_text(json.dumps(asdict(m), indent=2) + "\n")
    return manifest_path


def load(run_id: str, root: Path = MANIFEST_DIR) -> Manifest:
    """Read back the manifest written by `save` for `run_id`.

    A batch written before `removed_at` existed simply lacks the key, and takes the field's
    default — so a run launched by an older build still cleans.

    Raises:
        FileNotFoundError: no manifest for `run_id` under `root`.
    """
    data = json.loads(path(run_id, root).read_text())
    return Manifest(
        data["run_id"],
        data["orchestrator_branch"],
        [Batch(**b) for b in data["batches"]],
    )


def live_batches(root: Path = MANIFEST_DIR) -> dict[str, tuple[str, Batch]]:
    """Every batch id still standing across every run under `root`, to its run and record.

    "Still standing" means no `removed_at`: a batch cleaned in one run and relaunched in
    another must not be found through the cleaned entry, so only live entries are keyed and
    a later live one wins. Callers use this to refuse a duplicate launch, so a manifest it
    cannot read is skipped on its own rather than raising — one unreadable file must not
    refuse every launch on the host.

    Returns:
        `{batch id: (run_id, Batch)}` for every batch with no `removed_at`.
    """
    live: dict[str, tuple[str, Batch]] = {}
    if not root.is_dir():
        return live
    for manifest_file in sorted(root.glob("*.json")):
        try:
            data = json.loads(manifest_file.read_text())
            run_id = data["run_id"]
            batches = [Batch(**b) for b in data["batches"]]
        except OSError, ValueError, KeyError, TypeError:
            continue
        for b in batches:
            if not b.removed_at:
                live[b.batch] = (run_id, b)
    return live
