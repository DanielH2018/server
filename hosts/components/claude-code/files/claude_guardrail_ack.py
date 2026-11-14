#!/usr/bin/env python3
"""Post a Discord message for each change to a Claude Code guardrail file that nothing merged explains.

server#8250 slice 2. homelab-claude-guardrail-ack.timer runs this as root on both hosts. Each run
reads every file under the guardrail paths in the config, compares it with the copy the last run
kept, and classifies each change:

- explained by a package: dpkg owns the path, and the file matches the md5 recorded inside the
  `.deb` an apt source publishes for the installed version. apt checks that `.deb` against the
  signed index. A package built elsewhere and installed with `dpkg -i` matches its own md5sums,
  but not a published `.deb` of its version, so its change is posted with the installed
  version. Reconcile's record cannot make this split, because the postinst starts reconcile,
  which records the hand-installed version too;
- explained by the dotfiles: the path is under a user's home, and the file matches what chezmoi
  renders for that user from the dotfiles repo's `main`;
- Claude Code's own write: a settings.json whose changed top-level keys are all in
  `quiet_settings_keys`, the keys `/config` and `/model` write;
- explained as a copy: the path is under a `copies` entry's `dst`, and its content would be
  explained by one of the classes above at the matching path under that entry's `src`;
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

from host_lib import discord_post, write_check_textfile

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
    note: str = ""

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


@dataclass(frozen=True)
class Owner:
    """The installed package that owns a path, and what it records for that path."""

    package: str
    version: str
    md5: str


def dpkg_owner(path: str) -> Owner | None:
    """The package that owns `path` with its version and recorded md5, or None if none owns it."""
    owner = subprocess.run(["dpkg-query", "-S", path], capture_output=True, text=True)
    if owner.returncode != 0:
        return None
    package = owner.stdout.split(":", 1)[0].strip()
    sums = Path(f"/var/lib/dpkg/info/{package}.md5sums")
    if not sums.is_file():
        return None
    version = subprocess.run(
        ["dpkg-query", "-W", "-f", "${Version}", package],
        capture_output=True,
        text=True,
    ).stdout.strip()
    digest = parse_md5sums(sums.read_text()).get(path)
    return Owner(package, version, digest) if digest is not None else None


_POLICY_VERSION = re.compile(r"^ (?:\*\*\*| {3}) (\S+) -?\d+$")
_POLICY_SOURCE = re.compile(r"^ {8}-?\d+ (.+)$")


def published_versions(policy: str) -> list[str]:
    """The versions an `apt-cache policy` table lists from a source other than dpkg's status file, newest first."""
    found: list[str] = []
    version = None
    for line in policy.splitlines():
        if m := _POLICY_VERSION.match(line):
            version = m.group(1)
        elif (m := _POLICY_SOURCE.match(line)) and version is not None:
            if m.group(1) != "/var/lib/dpkg/status" and version not in found:
                found.append(version)
    return found


def parse_md5sums(text: str) -> dict[str, str]:
    """A package's md5sums file as {absolute path: md5}."""
    sums = {}
    for line in text.splitlines():
        digest, _, name = line.partition("  ")
        if name:
            sums["/" + name] = digest
    return sums


def apt_published_md5s(package: str, version: str) -> dict[str, str]:
    """The md5sums inside the `.deb` an apt source serves for `package` at `version`.

    Raises:
        subprocess.SubprocessError: The download or the read failed.
        OSError: The download left no single `.deb`.
    """
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [
                "apt-get",
                "download",
                "-q",
                "-o",
                "APT::Sandbox::User=root",
                f"{package}={version}",
            ],
            cwd=tmp,
            capture_output=True,
            timeout=300,
            check=True,
        )
        debs = list(Path(tmp).glob("*.deb"))
        if len(debs) != 1:
            raise OSError(
                f"apt-get download {package}={version} left {len(debs)} .deb files"
            )
        info = subprocess.run(
            ["dpkg-deb", "--info", str(debs[0]), "md5sums"],
            capture_output=True,
            text=True,
            check=True,
        )
    return parse_md5sums(info.stdout)


def apt_published(package: str) -> list[str]:
    """The versions of `package` the apt sources publish, newest first."""
    proc = subprocess.run(
        ["apt-cache", "policy", package],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return published_versions(proc.stdout)


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
        owner: The installed package that owns a path, or None when no package owns it.
        published: The versions of a package the apt sources publish, newest first.
        published_md5s: The md5sums of the published `.deb` for (package, version). Raises when
            the download fails, so the run reports down and retries rather than posting.
        render: What chezmoi renders for (user, chezmoi binary, source, target), or None.
        sync: Bring the dotfiles clone to `main` from (repo, clone); returns its commit.
    """

    owner: Callable[[str], Owner | None] = dpkg_owner
    published: Callable[[str], list[str]] = apt_published
    published_md5s: Callable[[str, str], dict[str, str]] = apt_published_md5s
    render: Callable[[str, str, str, str], bytes | None] = chezmoi_render
    sync: Callable[[str, str], str] = sync_dotfiles


HOST_BOUNDARIES = Boundaries()


def classify(
    change: Change, config: dict, tools: Boundaries, dotfiles: Callable[[], str]
) -> Change:
    """The change with its reason filled in when something explains it."""
    digest = hashlib.md5(change.new or b"").hexdigest()
    owner = tools.owner(change.path) if change.new is not None else None
    if owner is not None and owner.md5 == digest:
        published = tools.published(owner.package)
        if owner.version not in published:
            newest = published[0] if published else "none"
            note = (
                f"{owner.package} {owner.version} is installed, and no apt source "
                f"serves it; the newest published version is {newest}"
            )
        elif (
            tools.published_md5s(owner.package, owner.version).get(change.path)
            == digest
        ):
            return Change(change.path, change.old, change.new, "package")
        else:
            note = (
                f"{owner.package} {owner.version} is installed, and the file differs "
                "from the one in the published package of that version"
            )
        change = Change(change.path, change.old, change.new, note=note)
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
    for copy in config.get("copies", []):
        dst = copy["dst"].rstrip("/")
        if change.path != dst and not change.path.startswith(dst + "/"):
            continue
        source = copy["src"].rstrip("/") + change.path[len(dst) :]
        at_source = classify(
            Change(source, None, change.new), {**config, "copies": []}, tools, dotfiles
        )
        if at_source.explained:
            return Change(
                change.path,
                change.old,
                change.new,
                f"copy of {source} ({at_source.reason})",
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
        note = f" ({change.note})" if change.note else ""
        parts.append(
            f"`{change.path}` {state}{note}\n```diff\n{masked_diff(change)}\n```"
        )
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
    write_check_textfile(
        f"{textfile_dir}/{CHECK}.prom",
        CHECK,
        success=ok,
        deadline_s=config["deadline_s"],
        started=started,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main(sys.argv))
