#!/usr/bin/env python3
"""Regenerate the reference pages, then build the MkDocs site.

WHY ONE SCRIPT. The docs-refresh cron calls this and nothing else. Every decision about
what to generate, in what order, and what to do when one fails lives here in Python,
where it is testable -- not in a cron job line, where it is not.

FAILURE POLICY. A generator that fails is logged and skipped, and the site is built
anyway. This is the same reasoning the infra-map cron already records: a failed run
leaves the previous page in place rather than corrupting anything, and every page carries
the timestamp its content was last built from, so a generator that stops working surfaces
as one visibly stale page. Aborting the run instead would make every page stale to hide
that one page is. The exit code still reports the failure; the site build is not
conditional on it.

FRESHNESS IS TWO SIGNALS. The committed frontmatter says when a page's CONTENT last
changed -- the generators write through docs_provenance.write_if_body_changed, so an
unchanged page is not rewritten and the cron does not commit. When the CRON last ran goes
into the built site as build-info.json, which is never committed. Collapsing them into
one field would mean rewriting every page on every run, and ~730 commits a year for no
content change.

Usage::

    uv run python scripts/build_docs.py                          # default site dir
    uv run python scripts/build_docs.py --site-dir /tmp/site
    uv run python scripts/build_docs.py --skip-generators        # rebuild only
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# (argv, output path relative to the repo root). Order is not significant -- no generator
# reads another's output. Every output must sit under docs/, which is what the hand-edit
# hook protects; test_build_docs.py asserts that.
GENERATORS: list[tuple[list[str], str]] = [
    (
        [
            "scripts/service_catalog.py",
            "--format",
            "markdown",
            "--out",
            "docs/reference/services.md",
        ],
        "docs/reference/services.md",
    ),
    (
        ["scripts/gen_reference_hosts.py", "--out", "docs/reference/hosts.md"],
        "docs/reference/hosts.md",
    ),
    (
        ["scripts/gen_reference_secrets.py", "--out", "docs/reference/secrets.md"],
        "docs/reference/secrets.md",
    ),
    (
        ["scripts/gen_reference_crons.py", "--out", "docs/reference/crons.md"],
        "docs/reference/crons.md",
    ),
    (
        [
            "scripts/gen_infra_map.py",
            "--format",
            "svg",
            "--out",
            "docs/assets/generated/infra-map.svg",
        ],
        "docs/assets/generated/infra-map.svg",
    ),
]


def run_generators() -> list[str]:
    """Run every generator. Returns the scripts that failed; never raises."""
    failed: list[str] = []
    for argv, out in GENERATORS:
        script = argv[0]
        (REPO / out).parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["uv", "run", "python", *argv],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if result.returncode != 0:
            failed.append(script)
            print(
                f"build_docs: {script} FAILED rc={result.returncode}", file=sys.stderr
            )
            print(result.stderr.strip()[:2000], file=sys.stderr)
        else:
            print(f"build_docs: {script} ok -> {out}")
    return failed


def _write_build_stamp(site: Path) -> None:
    """When the cron last ran, written into the SERVED site and never committed.

    This is the half of the freshness signal that proves the cron is alive. Keeping it
    out of the repo is what stops every run producing a commit; docs/index.md fetches it
    and degrades silently when it is missing.
    """
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    (site / "build-info.json").write_text(
        json.dumps({"built_at": stamp}, indent=2) + "\n"
    )


def build_site(site_dir: str) -> bool:
    """`mkdocs build --strict`, swapped into place atomically. True on success.

    --strict turns a broken internal link into a build failure. A docs site that silently
    serves dead links is the failure this whole system exists to prevent, so the
    strictness is not negotiable here.

    WHY NOT BUILD STRAIGHT INTO site_dir. mkdocs cleans its --site-dir first, so a direct
    build empties the tree the docs pod is serving and refills it over several seconds,
    and leaves it empty for good if the build then fails. So the build goes to a sibling
    and only replaces the live tree once it has succeeded.

    WHY THE CONTENTS ARE SYNCED RATHER THAN THE DIRECTORY RENAMED. site_dir is a hostPath
    mount, and a bind mount follows the directory INODE, not the path. Renaming a fresh
    directory over it leaves the pod mounted on the old inode -- which the cleanup then
    deletes, so nginx serves an empty tree and answers 403 until someone restarts the pod.
    Measured on 2026-08-24, by doing exactly that. rsync updates in place, so the inode
    the pod holds is the one that gets the new files; it also writes each file to a temp
    name and renames it within the destination, so a reader never sees a partial file.

    --chmod is set because this host's umask is 007, which would otherwise leave the tree
    0770/0660 and unreadable by any uid but the owner's. The pod runs as that uid today,
    so it would work by luck rather than by design.

    A failed build leaves the previous site serving, untouched.
    """
    final = Path(site_dir)
    staging = final.parent / f"{final.name}.new"

    result = subprocess.run(
        ["uv", "run", "mkdocs", "build", "--strict", "--site-dir", str(staging)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if result.returncode != 0:
        print(
            "build_docs: mkdocs build FAILED (previous site left serving)",
            file=sys.stderr,
        )
        print(result.stderr.strip()[:4000], file=sys.stderr)
        shutil.rmtree(staging, ignore_errors=True)
        return False

    _write_build_stamp(staging)
    final.mkdir(parents=True, exist_ok=True)

    # Trailing slash on the source: copy the CONTENTS of staging into final, not staging
    # itself as a subdirectory.
    synced = subprocess.run(
        [
            "rsync",
            "-a",
            "--delete",
            "--chmod=D755,F644",
            f"{staging}/",
            f"{final}/",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    shutil.rmtree(staging, ignore_errors=True)
    if synced.returncode != 0:
        print("build_docs: rsync into the served directory FAILED", file=sys.stderr)
        print(synced.stderr.strip()[:2000], file=sys.stderr)
        return False

    print(f"build_docs: site built -> {final}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--site-dir",
        default="/home/ubuntu/docs-site",
        help="where mkdocs writes the built site (the hostPath the docs pod serves)",
    )
    parser.add_argument(
        "--skip-generators",
        action="store_true",
        help="build the site from the committed pages without regenerating them",
    )
    args = parser.parse_args(argv)

    failed = [] if args.skip_generators else run_generators()
    built = build_site(args.site_dir)

    if failed:
        print(
            f"build_docs: {len(failed)} generator(s) failed: {', '.join(failed)}",
            file=sys.stderr,
        )
    return 0 if built and not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
