"""The run manifest: ~/.claude/fanout/<run-id>.json, outside every checkout — spec §4."""

import dataclasses
import json
import os
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.


from dev.fanout_lib.sizing import DEFAULT
from dev.fanout_lib.target import SERVER

MANIFEST_DIR = Path.home() / ".claude" / "fanout"
# Where `retire` moves a run whose every batch is removed. A subdirectory, so the
# non-recursive `*.json` globs (`all_runs`, the SessionStart banner) never list it.
RETIRED = "retired"
# The manifest key that counts a batch's conflict resumes; `fanout_lib.resume.MAX_RESUMES`
# bounds it.
CONFLICT_RESUMES = "conflict_resumes"
# The manifest key holding the `kept:` line a clean last read for a batch, and how many cleans
# in a row read it.
SWEEP_KEPT = "sweep_kept"
# How many cleans in a row may read the same `kept:` line before the worktree sweep stops
# running that batch's leg. At the lander's six-hour `CLEAN_INTERVAL_S`, four is one day.
SWEEP_KEPT_LIMIT = 4


class KeptStreak(NamedTuple):
    """A batch's run of cleans that read the same `kept:` line.

    Attributes:
        line: the `kept:` line, or "" when no clean kept the batch.
        passes: how many cleans in a row read `line`.
        merged_pr: the URL of the branch's newest merged PR when the sweep stopped on the
            batch, "" for none, or None while it is not recorded.
    """

    line: str = ""
    passes: int = 0
    merged_pr: str | None = None

# A value a shell reads as itself wherever a command names it: as a bare word, inside single
# or double quotes, and inside a systemd `Exec` line. No leading `-`, which ssh and git read
# as an option.
_PLAIN_WORD = re.compile(r"[A-Za-z0-9_+./:@][A-Za-z0-9_+./:@-]*")


def _refuse_unless_plain_words(record: object, names: tuple[str, ...]) -> None:
    """Raise ValueError for the first of `record`'s `names` that is not a plain word."""
    for name in names:
        value = getattr(record, name)
        if not isinstance(value, str) or not _PLAIN_WORD.fullmatch(value):
            raise ValueError(
                f"{type(record).__name__.lower()} {name} {value!r} is not a plain word "
                "(letters, digits and _+./:@-, with no leading -)"
            )


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
        cited_paths: the files the batch's issues cite, as `launch` resolved them when it
            briefed the batch. `launch_gates.refuse_live_shared_files` compares a new
            launch against these. None for a batch saved before the field existed, whose
            issue bodies that gate fetches instead.
        excused_paths: the files this batch cites that a live batch in another run also
            held at launch, each passed with `--allow-shared-file`.
        extra: the keys of the manifest entry that are not fields here, which a newer
            build wrote. `to_json` writes them back, so a rewrite by this build loses
            nothing the newer build recorded. Not part of equality. It also holds
            `CONFLICT_RESUMES` and `SWEEP_KEPT`, which only some entries carry, so every other
            entry stays readable by a build that constructs a batch from its keys.
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
    cited_paths: tuple[str, ...] | None = None
    excused_paths: tuple[str, ...] = ()
    extra: dict = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        """Refuse a `batch`, `host`, `worktree`, `branch` or `unit` that is not a plain word.

        The fan-out commands name these five, unquoted, in shell text they run on `host`. A
        manifest is a file on disk, so the check is here, where every batch is built, and no
        command builder quotes or checks one.

        Raises:
            ValueError: one of the five is not a string of letters, digits and `_+./:@-`, or
                starts with `-`.
        """
        _refuse_unless_plain_words(
            self, ("batch", "host", "worktree", "branch", "unit")
        )

    @classmethod
    def from_json(cls, data: dict) -> Batch:
        """Build a batch from its manifest entry, keeping the keys that are not fields.

        Raises:
            TypeError: `data` is not an object, or lacks a field that has no default.
            ValueError: a field a command names is not a plain word.
        """
        if not isinstance(data, dict):
            raise TypeError(f"a batch entry is an object, not {type(data).__name__}")
        known = {f.name for f in fields(cls)} - {"extra"}
        given = {k: v for k, v in data.items() if k in known}
        for name in ("cited_paths", "excused_paths"):
            if isinstance(given.get(name), list):
                given[name] = tuple(given[name])
        return cls(
            **given,
            extra={k: v for k, v in data.items() if k not in known},
        )

    @property
    def conflict_resumes(self) -> int:
        """How many times `fanout.py place resume` restarted the batch for a merge conflict."""
        return int(self.extra.get(CONFLICT_RESUMES) or 0)

    def resumed_for_conflict(self) -> Batch:
        """This batch with one more conflict resume counted."""
        counted = {**self.extra, CONFLICT_RESUMES: self.conflict_resumes + 1}
        return dataclasses.replace(self, extra=counted)

    @property
    def sweep_kept(self) -> KeptStreak:
        """The batch's streak of identical `kept:` lines, empty when none is recorded."""
        rec = self.extra.get(SWEEP_KEPT) or {}
        merged = rec.get("merged_pr")
        return KeptStreak(
            line=str(rec.get("line") or ""),
            passes=int(rec.get("passes") or 0),
            merged_pr=None if merged is None else str(merged),
        )

    @property
    def sweep_stopped(self) -> bool:
        """Whether `SWEEP_KEPT_LIMIT` cleans in a row kept the batch with the same line."""
        return self.sweep_kept.passes >= SWEEP_KEPT_LIMIT

    def kept_by_clean(self, line: str) -> Batch:
        """This batch with a clean's `kept:` line counted: one more pass if it repeats, else one."""
        streak = self.sweep_kept
        repeated = line == streak.line
        counted = KeptStreak(
            line,
            streak.passes + 1 if repeated else 1,
            streak.merged_pr if repeated else None,
        )
        return self._with_streak(counted)

    def stopped_at_merge(self, merged_pr: str) -> Batch:
        """This batch with the branch's newest merged PR recorded as it stood at the stop."""
        return self._with_streak(self.sweep_kept._replace(merged_pr=merged_pr))

    def _with_streak(self, streak: KeptStreak) -> Batch:
        return dataclasses.replace(
            self, extra={**self.extra, SWEEP_KEPT: streak._asdict()}
        )

    def kept_streak_cleared(self) -> Batch:
        """This batch with no `kept:` streak recorded."""
        extra = {k: v for k, v in self.extra.items() if k != SWEEP_KEPT}
        return dataclasses.replace(self, extra=extra)

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

    def __post_init__(self) -> None:
        """Refuse a `run_id` or `orchestrator_branch` that is not a plain word.

        Raises:
            ValueError: either is not a string of letters, digits and `_+./:@-`, or starts
                with `-`.
        """
        _refuse_unless_plain_words(self, ("run_id", "orchestrator_branch"))

    @classmethod
    def from_json(cls, data: dict) -> Manifest:
        """Build a run from the object `to_json` wrote.

        Raises:
            KeyError: `data` lacks one of the three keys.
            TypeError: `data` or one of its batches has the wrong shape.
            ValueError: the run or one of its batches holds a field that is not a plain word.
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


def unused_run_id(now: datetime, root: Path = MANIFEST_DIR) -> str:
    """The first run id at or after `now` that no manifest under `root` has.

    Two launches named in one second would share an id, and the second would save over the
    first's manifest. The caller holds the launch lock (`fanout_lib.launch_lock`), so no
    other launch can take the id between this read and its own first save.
    """
    while exists(new_run_id(now), root):
        now += timedelta(seconds=1)
    return new_run_id(now)


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
        ValueError: the file is not JSON, or holds a field that is not a plain word.
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


def standing_batches(
    repo: str, root: Path = MANIFEST_DIR
) -> list[tuple[Manifest, Batch]]:
    """Every batch in `repo` with no `removed_at`, across every run under `root`, with its run.

    Filtered by repo because a batch id is only issue numbers, and a path is only a path:
    neither means the same thing in another repo's register or tree.
    """
    return [
        (run, b)
        for run in all_runs(root)
        for b in run.batches
        if not b.removed_at and b.repo == repo
    ]


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
