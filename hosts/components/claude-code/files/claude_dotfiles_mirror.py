#!/usr/bin/env python3
"""Keep a root-owned mirror of the dotfiles repo and record the newest tag the operator signed.

dotfiles#967 D13, the root half of the T2 config source. homelab-claude-dotfiles-mirror.timer
runs this as root. Each run fetches the dotfiles repo into a bare mirror that only root writes,
then takes the newest tag by creation date, the higher version name breaking a tie. The tag is
recorded as verified only when all of these hold:

- it is an annotated tag whose signature verifies against `allowed_signers`, which holds only
  the operator's key. git runs with no system or global config, a `HOME` this unit controls,
  `ssh-keygen` as the signature program and no fsmonitor, so nothing in the mirror or the
  environment can change how the signature is checked;
- its commit descends from the commit the last verified tag named, so an older signed tag
  cannot roll T2's config back.

A verified tag's name and commit SHA go to `verified.json` in the state directory. A later
render reads only that SHA. A run that finds no tag yet records nothing and reports up.

Usage: claude_dotfiles_mirror.py CONFIG
Exit 0 = up, 1 = down. Each run logs one `status=<up|down>` line under the
claude-dotfiles-mirror tag and writes claude-dotfiles-mirror.prom to TEXTFILE_DIR.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from host_lib import write_textfile

CHECK = "claude-dotfiles-mirror"
GIT_TIMEOUT_S = 300


def git(state: Path, signers: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git with no system or global config and a `HOME` under `state`.

    Every other `GIT_*` variable is dropped, so none can point git at another repository or
    config. ssh-keygen is named by its full path, so the inherited `PATH` decides nothing the
    verification reads.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env |= {
        "HOME": str(state / "home"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
    config = [
        "-c", "core.fsmonitor=false",
        "-c", "gpg.ssh.program=/usr/bin/ssh-keygen",
        "-c", f"gpg.ssh.allowedSignersFile={signers}",
    ]  # fmt: skip
    return subprocess.run(
        ["git", *config, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
        check=False,
    )


def _last_line(proc: subprocess.CompletedProcess[str]) -> str:
    lines = (proc.stderr or proc.stdout).strip().splitlines()
    return lines[-1] if lines else f"exit {proc.returncode}"


def run(config: dict, state: Path) -> tuple[bool, str]:
    """Fetch, pick the newest tag, verify it and record it; (up, the status line's detail)."""
    signers = config["allowed_signers"]
    mirror = state / "mirror.git"
    (state / "home").mkdir(parents=True, exist_ok=True)
    if not mirror.exists():
        cloned = git(
            state, signers, "clone", "--mirror", "--quiet", config["repo"], str(mirror)
        )
        if cloned.returncode != 0:
            return False, f"cannot clone {config['repo']}: {_last_line(cloned)}"
    at = ("-C", str(mirror))
    fetched = git(
        state, signers, *at, "fetch", "--quiet", "--prune", "--prune-tags", "origin"
    )
    if fetched.returncode != 0:
        return False, f"cannot fetch {config['repo']}: {_last_line(fetched)}"
    tags = git(
        state, signers, *at,
        "for-each-ref", "--sort=-v:refname",
        "--sort=-creatordate", "--format=%(refname:short)", "refs/tags",
    ).stdout.split()  # fmt: skip
    if not tags:
        return True, "no tag yet; nothing verified"
    tag = tags[0]
    kind = git(state, signers, *at, "cat-file", "-t", f"refs/tags/{tag}").stdout.strip()
    if kind != "tag":
        return False, f"tag {tag} is a lightweight tag, which carries no signature"
    verified = git(state, signers, *at, "verify-tag", f"refs/tags/{tag}")
    if verified.returncode != 0:
        return (
            False,
            f"tag {tag} does not verify against {signers}: {_last_line(verified)}",
        )
    commit = git(
        state, signers, *at, "rev-parse", f"refs/tags/{tag}^{{commit}}"
    ).stdout.strip()
    record_path = state / "verified.json"
    last = json.loads(record_path.read_text()) if record_path.exists() else None
    if last and last["commit"] == commit:
        return True, f"tag {tag} at {commit[:12]} already verified"
    if last:
        ancestor = git(
            state, signers, *at, "merge-base", "--is-ancestor", last["commit"], commit
        )
        if ancestor.returncode != 0:
            return False, (
                f"tag {tag} at {commit[:12]} does not descend from the last verified tag "
                f"{last['tag']} at {last['commit'][:12]}; kept {last['tag']}"
            )
    record = {"tag": tag, "commit": commit, "verified_at": int(time.time())}
    write_textfile(record_path, json.dumps(record) + "\n")
    return True, f"verified tag {tag} at {commit[:12]}"


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv[1:] in (["-h"], ["--help"]) else 2
    config = json.loads(Path(argv[1]).read_text())
    started = time.time()
    try:
        ok, line = run(config, Path(config["state_dir"]))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
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
    sys.exit(main(sys.argv))
