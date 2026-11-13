#!/usr/bin/env python3
"""Post a Discord message for each change to a Claude Code guardrail file that nothing merged explains.

server#8250 slice 2. homelab-claude-guardrail-ack.timer runs this as root on both hosts. Each run
reads every file under the guardrail paths in the config, compares it with the copy the last run
kept, and classifies each change:

- explained by a package: dpkg owns the path, and the file matches the installed package's md5;
- explained by the dotfiles: the path is under a user's home, and the file matches what chezmoi
  renders for that user from the dotfiles repo's `main`;
- Claude Code's own write: a settings.json whose changed top-level keys are all in
  `quiet_settings_keys`, the keys `/config` and `/model` write;
- otherwise unexplained, and posted with its diff. A value that looks like a credential is masked.

Every change is acknowledged once: the kept copy moves to the new content whatever the class. The
first run keeps a copy of every file and posts nothing. Python bytecode and pytest's cache under a
hooks directory are skipped, because the interpreter and pytest rewrite them.

Usage: claude_guardrail_ack.py CONFIG
Exit 0 = the run read every path and delivered or spooled its post; 1 = it did not. Each run logs
one `status=<up|down>` line under the claude-guardrail-ack tag and writes
claude-guardrail-ack.prom to TEXTFILE_DIR.
"""

import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from host_lib import discord_post, write_textfile

CHECK = "claude-guardrail-ack"
_SKIP_PARTS = {"__pycache__", ".pytest_cache"}
_SKIP_SUFFIXES = (".pyc",)
_SECRET_WORD = re.compile(
    r"(?i)token|secret|passw|api[_-]?key|authori[sz]ation|bearer|webhook|credential|cookie"
)
_SECRET_VALUE = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
_MASK = "<masked>"


@dataclass(frozen=True)
class Change:
    """One file whose content moved since the last run."""

    path: str
    old: bytes | None
    new: bytes | None
    reason: str = ""

    @property
    def explained(self) -> bool:
        return bool(self.reason)


def files_under(paths: list[str]) -> dict[str, bytes]:
    """The content of every regular file each path names or holds, by absolute path."""
    found = {}
    for raw in paths:
        root = Path(raw)
        candidates = (
            [root]
            if root.is_file()
            else sorted(root.rglob("*"))
            if root.is_dir()
            else []
        )
        for path in candidates:
            if not path.is_file() or path.is_symlink():
                continue
            if _SKIP_PARTS & set(path.parts) or path.name.endswith(_SKIP_SUFFIXES):
                continue
            found[str(path)] = path.read_bytes()
    return found


def dpkg_md5(path: str) -> str | None:
    """The md5 the owning package records for `path`, or None when no package owns it."""
    owner = subprocess.run(["dpkg-query", "-S", path], capture_output=True, text=True)
    if owner.returncode != 0:
        return None
    package = owner.stdout.split(":", 1)[0].strip()
    sums = Path(f"/var/lib/dpkg/info/{package}.md5sums")
    if not sums.is_file():
        return None
    for line in sums.read_text().splitlines():
        digest, _, name = line.partition("  ")
        if "/" + name == path:
            return digest
    return None


def chezmoi_render(user: str, chezmoi: str, source: str, target: str) -> bytes | None:
    """What chezmoi renders for `target` as `user` from `source`, or None if it manages no such file."""
    with tempfile.TemporaryDirectory() as state:
        os.chmod(state, 0o777)
        proc = subprocess.run(
            [
                "runuser",
                "-u",
                user,
                "--",
                chezmoi,
                "--source",
                source,
                "--persistent-state",
                f"{state}/state.boltdb",
                "cat",
                target,
            ],
            capture_output=True,
            timeout=120,
        )
    return proc.stdout if proc.returncode == 0 else None


def sync_dotfiles(repo: str, clone: str) -> str:
    """Bring `clone` to the repo's `main` and return the commit it holds."""
    git = ["git", "-c", "safe.directory=*", "-C", clone]
    if not Path(clone, ".git").is_dir():
        Path(clone).parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--quiet", repo, clone], check=True, timeout=300
        )
    subprocess.run(
        [*git, "fetch", "--quiet", "origin", "main"], check=True, timeout=300
    )
    subprocess.run(
        [*git, "reset", "--quiet", "--hard", "origin/main"], check=True, timeout=60
    )
    os.chmod(clone, 0o755)
    return subprocess.run(
        [*git, "rev-parse", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def quiet_settings_change(
    old: bytes | None, new: bytes | None, quiet: set[str]
) -> bool:
    """Whether a settings.json change touches only the keys Claude Code itself writes."""
    try:
        before, after = json.loads(old or b"{}"), json.loads(new or b"{}")
    except ValueError:
        return False
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    changed = {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
    return bool(changed) and changed <= quiet


def mask_line(line: str) -> str:
    """The line with any credential-shaped value replaced, so a posted diff carries none."""
    if _SECRET_WORD.search(line):
        m = re.search(r"[:=]", line)
        return (line[: m.end()] + " " + _MASK) if m else _MASK
    return _SECRET_VALUE.sub(_MASK, line)


def masked_diff(change: Change, limit: int = 40) -> str:
    """A unified diff of the change, masked and cut to `limit` lines."""

    def lines(blob):
        if blob is None:
            return []
        return blob.decode("utf-8", "replace").splitlines()

    diff = list(
        difflib.unified_diff(
            lines(change.old), lines(change.new), "before", "after", lineterm="", n=1
        )
    )[2:]
    body = [mask_line(d) for d in diff[:limit]]
    if len(diff) > limit:
        body.append(f"... {len(diff) - limit} more lines")
    return "\n".join(body)


@dataclass(frozen=True)
class Boundaries:
    """The calls a run makes outside this process, so a test can replace them.

    Attributes:
        md5: The installed package's md5 for a path, or None when no package owns it.
        render: What chezmoi renders for (user, chezmoi binary, source, target), or None.
        sync: Bring the dotfiles clone to `main` from (repo, clone); returns its commit.
    """

    md5: Callable[[str], str | None] = dpkg_md5
    render: Callable[[str, str, str, str], bytes | None] = chezmoi_render
    sync: Callable[[str, str], str] = sync_dotfiles


HOST_BOUNDARIES = Boundaries()


def classify(
    change: Change, config: dict, tools: Boundaries, dotfiles: Callable[[], str]
) -> Change:
    """The change with its reason filled in when something explains it."""
    if change.new is not None:
        digest = tools.md5(change.path)
        if digest is not None and digest == hashlib.md5(change.new).hexdigest():
            return Change(change.path, change.old, change.new, "package")
    if change.path.endswith("/settings.json") and quiet_settings_change(
        change.old, change.new, set(config["quiet_settings_keys"])
    ):
        return Change(
            change.path, change.old, change.new, "Claude Code's own settings write"
        )
    for home in config["homes"]:
        if change.path.startswith(home["home"].rstrip("/") + "/"):
            commit = dotfiles()
            rendered = tools.render(
                home["user"], home["chezmoi"], config["dotfiles_clone"], change.path
            )
            if rendered == change.new:
                return Change(
                    change.path, change.old, change.new, f"dotfiles main {commit}"
                )
    return change


def changes(
    previous: dict[str, str], current: dict[str, bytes], snapshot: Path
) -> list[Change]:
    """Each file added, changed or removed since the index `previous` was written."""
    found = []
    for path in sorted(previous.keys() | current.keys()):
        now = current.get(path)
        if now is not None and previous.get(path) == hashlib.sha256(now).hexdigest():
            continue
        kept = snapshot / previous[path] if path in previous else None
        old = kept.read_bytes() if kept is not None and kept.is_file() else None
        found.append(Change(path, old, now))
    return found


def message(host: str, unexplained: list[Change]) -> str:
    """The Discord post for a run's unexplained changes."""
    parts = [
        f"**{host}**: {len(unexplained)} Claude Code guardrail change(s) that nothing merged explains."
    ]
    for change in unexplained:
        state = (
            "added"
            if change.old is None
            else "removed"
            if change.new is None
            else "changed"
        )
        parts.append(f"`{change.path}` {state}\n```diff\n{masked_diff(change)}\n```")
    parts.append(
        'Read the audit record: `{job="audit"} |= "claude-guardrail"` in loki-homelab.'
    )
    return "\n".join(parts)


def run(
    config: dict,
    state: Path,
    host: str,
    post: Callable[[str], bool],
    tools: Boundaries = HOST_BOUNDARIES,
) -> tuple[bool, str]:
    """One pass. Returns (ok, the verdict line)."""
    snapshot = state / "snapshot"
    snapshot.mkdir(parents=True, exist_ok=True, mode=0o700)
    index_path = state / "index.json"
    current = files_under(config["paths"])
    if not index_path.is_file():
        _store(current, snapshot, index_path)
        return True, f"baselined {len(current)} files; nothing posted"

    synced: list[str] = []

    def dotfiles() -> str:
        if not synced:
            synced.append(tools.sync(config["dotfiles_repo"], config["dotfiles_clone"]))
        return synced[0]

    found = [
        classify(c, config, tools, dotfiles)
        for c in changes(json.loads(index_path.read_text()), current, snapshot)
    ]
    unexplained = [c for c in found if not c.explained]
    delivered = post(message(host, unexplained)) if unexplained else True
    if delivered:
        _store(current, snapshot, index_path)
    summary = (
        f"{len(current)} files, {len(found)} changed, {len(found) - len(unexplained)} explained, "
        f"{len(unexplained)} posted"
    )
    if not delivered:
        return False, f"{summary}; the post failed and is retried next run"
    return True, summary


def _store(current: dict[str, bytes], snapshot: Path, index_path: Path) -> None:
    """Keep a copy of each file by its sha256, and the path-to-sha256 index."""
    index = {}
    for path, blob in current.items():
        digest = hashlib.sha256(blob).hexdigest()
        target = snapshot / digest
        if not target.exists():
            target.write_bytes(blob)
            os.chmod(target, 0o600)
        index[path] = digest
    for stale in set(p.name for p in snapshot.iterdir()) - set(index.values()):
        (snapshot / stale).unlink()
    tmp = index_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, sort_keys=True))
    os.chmod(tmp, 0o600)
    tmp.replace(index_path)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv[1:] in (["-h"], ["--help"]) else 2
    config = json.loads(Path(argv[1]).read_text())
    host = os.uname().nodename
    webhook = (
        Path(os.environ["CREDENTIALS_DIRECTORY"], "discord_webhook").read_text().strip()
    )

    def post(content: str) -> bool:
        return discord_post(webhook, content, "homelab-claude-guardrail-ack")

    started = time.time()
    try:
        ok, line = run(config, Path(config["state_dir"]), host, post)
    except (OSError, subprocess.SubprocessError) as exc:
        ok, line = False, f"run failed: {exc}"
    print(f"status={'up' if ok else 'down'} {line}", flush=True)
    textfile_dir = os.environ.get("TEXTFILE_DIR", "/var/lib/node-exporter-textfile")
    write_textfile(
        f"{textfile_dir}/{CHECK}.prom",
        "".join(
            f'{name}{{check="{CHECK}"}} {value}\n'
            for name, value in (
                ("homelab_check_success", int(ok)),
                ("homelab_check_last_run_timestamp_seconds", int(time.time())),
                ("homelab_check_deadline_seconds", config["deadline_s"]),
                ("homelab_check_duration_seconds", int(time.time() - started)),
            )
        ),
    )
    return 0 if ok else 1


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main(sys.argv))
