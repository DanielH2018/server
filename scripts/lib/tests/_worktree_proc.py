"""A fake /proc entry for another uid's process, shared by the `lib.worktrees` tests."""

from pathlib import Path


def _unreadable_proc(root: Path, pid: int, uid: int, cgroup: str) -> Path:
    """A fake /proc entry the way another uid's process looks: `status` and `cgroup`
    readable, `cwd` and `environ` refused (here: absent, which raises OSError the same way)."""
    entry = root / str(pid)
    entry.mkdir(parents=True)
    (entry / "status").write_text(f"Name:\tsleep\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
    (entry / "cgroup").write_text(f"0::{cgroup}\n")
    return root
