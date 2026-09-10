"""The run manifest: ~/.claude/fanout/<run-id>.json, outside every checkout — spec §4."""

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

MANIFEST_DIR = Path.home() / ".claude" / "fanout"


@dataclass(frozen=True)
class Batch:
    """One launched batch's placement and identity, as recorded in the run manifest."""

    batch: str
    host: str
    worktree: str
    branch: str
    unit: str
    issues: list[int]
    launched_at: str


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
    """Read back the manifest written by `save` for `run_id`."""
    data = json.loads(path(run_id, root).read_text())
    return Manifest(
        data["run_id"],
        data["orchestrator_branch"],
        [Batch(**b) for b in data["batches"]],
    )
