#!/usr/bin/env python3
"""Render the unattended sessions' Claude Code config from the verified dotfiles tag, then publish it.

The render half of the T2 config source. homelab-claude-config-render
runs both commands in one unit:

- `render`, as `claude-triage-render`, with no network: clone the commit `verified.json` names
  from the root mirror into the unit's runtime directory, and render the dotfiles repo's
  `settings.base.json` there with chezmoi and a root-owned `--config`. It writes `out/commit` and
  `out/settings.base.json` and nothing else.
- `publish`, as root through `ExecStartPost=+`: read those two files without following a link,
  check the commit against the record root reads itself, keep only the deny and auto-mode keys,
  and merge each overlay the host packages ship under OVERLAY_DIR over them. The results go to a
  new version directory, which replaces `current` with one rename, so a session never reads a
  half-published config.

A session holds the publish dir's lock shared from start to exit
(`claude_config_lock.hold_settings`). A publish takes it exclusive without waiting, and defers to
the next run while a session holds it, so neither the swap nor the prune runs under a session.

Usage: claude_config_render.py render|publish
Exit 0 = published, unchanged or deferred, 1 = refused. Each command logs one `status=` line.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from claude_config_lock import exclusive

RECORD = Path("/var/lib/homelab-claude-dotfiles-mirror/verified.json")
MIRROR = Path("/var/lib/homelab-claude-dotfiles-mirror/mirror.git")
STAGE = Path("/run/homelab-claude-config-render")
CHEZMOI = "/usr/lib/homelab/bin/chezmoi"
CHEZMOI_CONFIG = "/usr/lib/homelab/claude-config-render/chezmoi.toml"
OVERLAY_DIR = Path("/usr/lib/homelab/claude-config-render/overlays")
PUBLISH_DIR = Path("/var/lib/homelab-claude-config")
OUTPUTS = frozenset({"commit", "settings.base.json"})
MAX_BYTES = 4 * 1024 * 1024
# DECIDED: autoMode.allow stays out. Its clearances (land.sh, sops set, a force-with-lease
# push) are the operator's sessions', not an unattended session's.
AUTO_MODE_LISTS = ("soft_deny", "hard_deny", "environment")
KEEP_VERSIONS = 2
TIMEOUT_S = 300


class Refused(Exception):
    """The staged render, the record or an overlay fails a check, so nothing is published."""


def _git_env(home: Path, mirror: Path) -> dict[str, str]:
    """No system config and a global config naming only the mirror as safe.

    The mirror is root's, so git refuses it as dubious ownership. `-c safe.directory` does not
    reach the upload-pack a clone starts; a global config does.
    """
    home.mkdir(parents=True, exist_ok=True)
    gitconfig = home / "gitconfig"
    gitconfig.write_text(
        f"[safe]\n\tdirectory = {mirror}\n[core]\n\tfsmonitor = false\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return env | {
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_TERMINAL_PROMPT": "0",
    }


def _run(argv: list[str], env: dict[str, str]) -> None:
    proc = subprocess.run(
        argv, env=env, capture_output=True, text=True, timeout=TIMEOUT_S, check=False
    )
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        fatal = [line for line in lines if line.startswith(("fatal:", "chezmoi:"))]
        detail = (fatal or lines or [f"exit {proc.returncode}"])[0]
        raise Refused(f"{Path(argv[0]).name} failed: {detail}")


def read_record(record: Path = RECORD) -> str:
    """The commit SHA the mirror last verified.

    Raises:
        Refused: the record is unreadable or names no full SHA.
    """
    try:
        commit = json.loads(record.read_text())["commit"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise Refused(f"cannot read {record}: {exc}") from exc
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise Refused(f"{record} names no commit SHA")
    return commit


def render(
    stage: Path = STAGE,
    *,
    record: Path = RECORD,
    mirror: Path = MIRROR,
    chezmoi: str = CHEZMOI,
    config: str = CHEZMOI_CONFIG,
) -> str:
    """Clone the verified commit into `stage` and render its settings; return the commit."""
    commit = read_record(record)
    env = _git_env(stage / "home", mirror)
    src = stage / "src"
    _run(
        [
            "git",
            "clone",
            "--quiet",
            "--no-local",
            "--no-checkout",
            str(mirror),
            str(src),
        ],
        env,
    )
    _run(["git", "-C", str(src), "checkout", "--quiet", "--detach", commit], env)
    root_file = src / ".chezmoiroot"
    root = src / root_file.read_text().strip() if root_file.is_file() else src
    out = stage / "out"
    out.mkdir()
    _run(
        [
            chezmoi,
            "--source", str(src),
            "--config", config,
            "--persistent-state", str(stage / "chezmoistate.boltdb"),
            "--cache", str(stage / "cache"),
            "--no-tty",
            "execute-template",
            "--file", str(root / ".chezmoitemplates" / "settings.base.json"),
            "--output", str(out / "settings.base.json"),
        ],
        env,
    )  # fmt: skip
    (out / "commit").write_text(commit + "\n")
    return commit


def _read_staged(out_fd: int, name: str) -> str:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=out_fd)
    except OSError as exc:
        raise Refused(f"cannot open staged {name}: {exc}") from exc
    with os.fdopen(fd, "rb") as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise Refused(f"staged {name} is not a regular file")
        data = f.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise Refused(f"staged {name} is over {MAX_BYTES} bytes")
    return data.decode()


def read_stage(stage: Path, commit: str) -> dict:
    """The staged render's deny and auto-mode keys, checked against the verified commit.

    Raises:
        Refused: the stage holds anything but OUTPUTS, a link or an oversized file, a commit
            other than `commit`, or a render without the keys a session's floor needs.
    """
    try:
        out_fd = os.open(stage / "out", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise Refused(f"cannot open the staged output: {exc}") from exc
    try:
        names = set(os.listdir(out_fd))
        if names != OUTPUTS:
            raise Refused(
                f"staged output holds {sorted(names)}, expected {sorted(OUTPUTS)}"
            )
        staged_commit = _read_staged(out_fd, "commit").strip()
        text = _read_staged(out_fd, "settings.base.json")
    finally:
        os.close(out_fd)
    if staged_commit != commit:
        raise Refused(
            f"staged commit {staged_commit[:12]} is not the verified {commit[:12]}"
        )
    try:
        rendered = json.loads(text)
    except ValueError as exc:
        raise Refused(f"the render is not JSON: {exc}") from exc
    return floor(rendered)


def _strings(value: object, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise Refused(f"{where} is not a list of strings")
    return value


def floor(rendered: dict) -> dict:
    """Keep `permissions.deny` and the auto-mode deny lists, environment and classifyAllShell.

    Raises:
        Refused: a kept key is missing or has the wrong type, or the deny list is empty.
    """
    try:
        deny = _strings(rendered["permissions"]["deny"], "permissions.deny")
        auto = rendered["autoMode"]
        kept = {k: _strings(auto[k], f"autoMode.{k}") for k in AUTO_MODE_LISTS}
        classify = auto["classifyAllShell"]
    except (KeyError, TypeError) as exc:
        raise Refused(f"the render lacks {exc}") from exc
    if not deny:
        raise Refused("the render's permissions.deny is empty")
    if not isinstance(classify, bool):
        raise Refused("autoMode.classifyAllShell is not a boolean")
    return {
        "permissions": {"deny": deny},
        "autoMode": kept | {"classifyAllShell": classify},
    }


def merge(base: dict, overlay: dict) -> dict:
    """One session's settings: the overlay's own keys, with its deny rules before the render's."""
    if "autoMode" in overlay:
        raise Refused(
            "an overlay may not carry autoMode; it comes from the verified tag"
        )
    perms = dict(overlay.get("permissions", {}))
    extra = _strings(perms.pop("deny", []), "overlay permissions.deny")
    deny = base["permissions"]["deny"]
    merged = {k: v for k, v in overlay.items() if k != "permissions"}
    merged["permissions"] = {
        "deny": extra + [r for r in deny if r not in extra],
        **perms,
    }
    merged["autoMode"] = base["autoMode"]
    return merged


def _versions(publish_dir: Path) -> list[Path]:
    versions = publish_dir / "versions"
    return sorted(versions.iterdir()) if versions.is_dir() else []


def _current_files(publish_dir: Path) -> dict[str, bytes] | None:
    current = publish_dir / "current"
    if not current.is_dir():
        return None
    return {p.name: p.read_bytes() for p in current.iterdir()}


def publish(
    stage: Path = STAGE,
    *,
    record: Path = RECORD,
    overlay_dir: Path = OVERLAY_DIR,
    publish_dir: Path = PUBLISH_DIR,
) -> str:
    """Publish the staged render with each overlay; return the status line's detail."""
    commit = read_record(record)
    base = read_stage(stage, commit)
    overlays = sorted(overlay_dir.glob("*.json"))
    if not overlays:
        raise Refused(f"no overlay under {overlay_dir}")
    files = {
        p.name: (
            json.dumps(merge(base, json.loads(p.read_text())), indent=2) + "\n"
        ).encode()
        for p in overlays
    }
    files["source.json"] = (json.dumps({"commit": commit}) + "\n").encode()
    with exclusive(publish_dir) as held:
        if not held:
            return "deferred: a session holds the config"
        if _current_files(publish_dir) == files:
            return f"unchanged at {commit[:12]}"
        version = (
            publish_dir / "versions" / f"{time.strftime('%Y%m%dT%H%M%S')}-{commit[:12]}"
        )
        # The unit's UMask=0077 reaches this root step too, so every mode is set, not requested.
        version.mkdir(parents=True)
        version.parent.chmod(0o755)
        version.chmod(0o755)
        for name, data in files.items():
            (version / name).write_bytes(data)
            (version / name).chmod(0o644)
        link = publish_dir / "current.new"
        link.unlink(missing_ok=True)
        link.symlink_to(version.relative_to(publish_dir))
        link.replace(publish_dir / "current")
        for old in _versions(publish_dir)[:-KEEP_VERSIONS]:
            shutil.rmtree(old)
    return f"published {commit[:12]} for {', '.join(p.stem for p in overlays)}"


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in ("render", "publish"):
        print(__doc__)
        return 0 if argv[1:] in (["-h"], ["--help"]) else 2
    try:
        if argv[1] == "render":
            line = f"status=up rendered {render()[:12]}"
        else:
            line = f"status=up {publish()}"
        code = 0
    except (Refused, OSError, ValueError, subprocess.SubprocessError) as exc:
        line, code = f"status=down {argv[1]} refused: {exc}", 1
    print(line, flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
