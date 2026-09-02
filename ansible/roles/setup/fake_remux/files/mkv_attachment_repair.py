#!/usr/bin/env python3
"""mkv attachment-name repair — sweep the library and rename attachments ffmpeg 7.1 rejects.

The one-off repair of 2026-08-29 fixed the 21 files then present. This is the durable half: every
new mkv arriving from any path (Sonarr, Radarr, a manual copy) can carry the same bad attachment
name, and the symptom — burned-in-subtitle transcodes returning "source error" while direct play
works — points at Jellyfin rather than at the file, so it is expensive to rediscover.

# DECIDED: renaming the attachment is safe, and this is the load-bearing correctness claim. An ASS
# subtitle script references a font by its INTERNAL family name (the `Style:` line's fontname),
# never by the attachment's filename, so libass still matches the font after the header rewrite.
# mkvpropedit rewrites the header in place without touching or re-encoding any track.

Runs as a cron on daniel-box under `uv run --no-project --python <pin>` (host_python_version in
ansible/inventory/group_vars/all.yml), sharing the fake-remux lock — see the role's tasks. Writes
the same {ts,ok,msg} state file shape as the scan and reconciler; fake-remux-health.sh turns it
into a Kuma push.

Usage:
  mkv_attachment_repair.py                # sweep the configured root, repair, write state
  mkv_attachment_repair.py --dry-run      # report what it would rename, write no state
  mkv_attachment_repair.py --dry-run PATH # just these files
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mkv_attachment_logic as mal
from host_lib import atomic_write, discord_post, parse_env_file

CONFIG_PATH = os.environ.get("FAKE_REMUX_CONFIG", "/etc/autofix-fake-remux/config.env")
USER_AGENT = "autofix-mkv-attachments"
DISCORD_MARKER = "🔤 mkv-attachments:"


def load_config() -> dict:
    cfg = dict(os.environ)
    if os.path.exists(CONFIG_PATH):
        cfg.update(parse_env_file(CONFIG_PATH))
    return cfg


def log(*args) -> None:
    print("[%s]" % time.strftime("%Y-%m-%dT%H:%M:%S"), *args, flush=True)


def write_state(state_file: str, ok: bool, msg: str) -> None:
    atomic_write(
        state_file, json.dumps({"ts": int(time.time()), "ok": bool(ok), "msg": msg})
    )


def attachment_names(path: pathlib.Path, timeout: int) -> list[str]:
    """Attachment filenames as ffprobe reports them, in header order."""
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "t",
            "-show_entries",
            "stream_tags=filename",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    ).stdout
    return [n for n in out.splitlines() if n.strip()]


def repair_file(path: pathlib.Path, renames, timeout: int) -> str | None:
    """Rewrite the header, then re-probe. Returns None on success, else the failure reason.

    The re-probe is not ceremony: mkvpropedit exits 0 on a selector that matched nothing, so its
    return code alone cannot tell a rename from a no-op.
    """
    cmd = ["mkvpropedit", str(path), *mal.mkvpropedit_args(renames)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        return "%s: mkvpropedit rc=%d %s" % (
            path.name,
            r.returncode,
            (r.stderr or r.stdout).strip()[:200],
        )
    left = [n for n in attachment_names(path, timeout) if not mal.is_safe(n)]
    if left:
        return "%s: still unsafe after rewrite: %s" % (path.name, left)
    return None


def sweep(cfg, roots, apply_changes: bool):
    """(ok, msg). Walks every root, repairs what ffmpeg would reject, reports the counts."""
    timeout = int(cfg.get("MKV_ATTACHMENT_TIMEOUT", cfg.get("HTTP_TIMEOUT", "60")))
    webhook = cfg.get("ARR_DISCORD_WEBHOOK_URL", "")

    files = mal.iter_targets(roots)
    scanned = 0
    repaired = 0
    failures: list[str] = []

    for f in files:
        scanned += 1
        try:
            renames = mal.plan(attachment_names(f, timeout))
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            failures.append("%s: probe failed: %s" % (f.name, e))
            continue
        if not renames:
            continue
        log("%s" % f)
        for old, new in renames:
            log("    %r -> %r" % (old, new))
        if not apply_changes:
            repaired += 1
            continue
        try:
            reason = repair_file(f, renames, timeout)
        except (
            subprocess.CalledProcessError,  # the re-probe inside repair_file runs check=True
            subprocess.TimeoutExpired,
            OSError,
        ) as e:
            reason = "%s: rewrite failed: %s" % (f.name, e)
        if reason:
            failures.append(reason)
            log("    FAILED %s" % reason)
            continue
        repaired += 1
        log("    ok")
        line = "%s — renamed %d attachment(s) ffmpeg 7.1 rejects" % (
            f.name,
            len(renames),
        )
        discord_post(webhook, line, USER_AGENT, log=log, marker=DISCORD_MARKER)

    return mal.verdict(scanned, repaired, failures)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="report the renames without rewriting anything, and write no state file",
    )
    ap.add_argument(
        "paths",
        nargs="*",
        type=pathlib.Path,
        help="mkv files or directories to sweep; default is the configured media root",
    )
    args = ap.parse_args(argv)

    cfg = load_config()
    state_file = cfg.get(
        "MKV_ATTACHMENT_STATE_FILE",
        "/var/lib/autofix-fake-remux/mkv_attachment_state.json",
    )
    roots = args.paths or [
        pathlib.Path(cfg.get("HOST_DATA_ROOT", "/srv/media")) / "media"
    ]

    log(
        "mkv attachment repair starting (%s)" % ("dry run" if args.dry_run else "apply")
    )
    try:
        ok, msg = sweep(cfg, roots, apply_changes=not args.dry_run)
    except (
        Exception
    ) as e:  # a sweep that cannot run at all is this check's own failure -> page
        ok, msg = False, "mkv attachment repair error: %s" % e
    log("OK  " if ok else "DOWN", msg)
    if not args.dry_run:
        write_state(state_file, ok, msg)
    # 0 even on a DOWN verdict, matching fake_remux_scan.py: the state file is the signal, and the
    # cron's `|| logger` arm is for a wrapper that could not run at all. A DOWN is data to report.
    return 0


if __name__ == "__main__":
    sys.exit(main())
