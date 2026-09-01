#!/usr/bin/env python3
"""The provenance banner every generated documentation page opens with.

WHY THIS EXISTS. The generated pages under docs/reference/ carry no monitor, no
Healthchecks ping and no alert. That is deliberate, and it matches what the
infra-map cron already does: a failed run leaves the previous page in place rather
than corrupting anything, so the useful signal is not "the run failed" but "this
page is old".

TWO SIGNALS, NOT ONE. A stamp regenerated on every run proves the cron is alive. A
stamp that changes only with content keeps diffs meaningful. Those pull against each
other, and as one field the first wins: every run rewrites every page, the cron's
`git diff --cached` is never empty, and twice a day becomes ~730 commits a year for
no content change. So they are split:

  * generated_at / generated_sha, in the committed frontmatter, mean "when this
    page's CONTENT last changed". write_if_body_changed() is what makes that true.
  * "when the cron last ran" is written into the BUILT SITE by build_docs.py as
    build-info.json, and is never committed.

Every generator calls generated_banner() for the preamble and write_if_body_changed()
to write the result.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
from collections.abc import Callable, Sized
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.repo_paths import REPO  # noqa: E402

# git reads these from the environment in preference to its working directory, so a `cwd=`
# alone does not scope a git call. Inside a git hook they are both set and point at the repo
# running the hook, which made head_sha() report that repo's SHA for any path it was handed.
# Stripped rather than overridden: the caller's `repo` argument is the only thing that should
# decide which tree is read.
_GIT_ENV_OVERRIDES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")

# A generated page names the hook that protects it, because a reader of the rendered
# page cannot see prek.toml.
_DO_NOT_EDIT = (
    '!!! warning "Generated file — do not edit"\n'
    "    This page is rendered from the Ansible tree by `{source}`. Hand edits are\n"
    "    overwritten by the next run, and a prek hook rejects them at commit time.\n"
    "    To change what appears here, change the generator or the source it reads.\n"
)


def head_sha(repo: Path | None = None) -> str:
    """The short HEAD SHA, or "unknown" when git cannot answer.

    Never raises. The cron runs unattended, and a generator that dies because git
    is missing or the directory is not a repo is a worse outcome than a page whose
    provenance line reads "unknown".
    """
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_OVERRIDES}
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo or REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip() or "unknown"


def generated_banner(
    source: str,
    *,
    when: dt.datetime | None = None,
    sha: str | None = None,
) -> str:
    """YAML frontmatter plus a do-not-edit admonition, for the top of a page.

    `source` is the generator's repo-relative path. `when` and `sha` are injectable
    so tests do not depend on the clock or on the checkout.
    """
    stamp = when or dt.datetime.now(dt.timezone.utc)
    commit = sha if sha is not None else head_sha()
    iso = stamp.strftime("%Y-%m-%d %H:%M UTC")
    return (
        "---\n"
        f"generated_from: {source}\n"
        f"generated_at: {iso}\n"
        f"generated_sha: {commit}\n"
        "---\n\n" + _DO_NOT_EDIT.format(source=source) + "\n"
    )


def _body(text: str) -> str:
    """Everything after the frontmatter block.

    Splits on the first two '---' delimiters only. '---' is also Markdown for a
    horizontal rule, which generated pages use, and splitting on every occurrence
    would compare just the text above the first rule.
    """
    if not text.startswith("---\n"):
        return text
    parts = text.split("---", 2)
    return parts[2] if len(parts) == 3 else text


def write_if_body_changed(path: Path, content: str) -> bool:
    """Write `content` to `path` only if the body below the frontmatter differs.

    Returns True when it wrote.

    WHY. generated_at and generated_sha change on every run -- the clock moves, and
    HEAD moves whenever anyone merges anything. Writing unconditionally would make
    the docs-refresh cron commit on every run for no content change, which is the
    commit noise this design accepted only in exchange for reviewable diffs. A diff
    that is always a timestamp bump is not reviewable.

    The freshness signal is not lost, it is relocated: the frontmatter stamp means
    "when the content last changed", and "when the cron last ran" is written into
    the served site by build_docs.py without being committed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and _body(path.read_text()) == _body(content):
        return False
    path.write_text(content)
    return True


def md_cell(value: str) -> str:
    """A Markdown table cell that cannot split its own row.

    A literal pipe in a value adds a column silently -- the table still renders, just wrong,
    which is worse than failing. Several generators derive cell text from template text or
    docstrings, so nothing upstream stops one appearing.
    """
    return value.replace("|", "\\|")


def finish_generator(
    name: str,
    out: Path,
    rows: Sized,
    render: Callable[[Sized], str],
    noun: str,
) -> int:
    """The tail every reference-page generator shares: render, write if changed, report.

    Five ``gen_reference_*`` scripts carried this verbatim before it moved here. The argparse
    front half stays in each script because their arguments differ; only the part that has
    to stay identical -- the write policy and the one-line report the docs-refresh cron log
    is read through -- is shared.
    """
    wrote = write_if_body_changed(out, render(rows))
    print(f"{name}: {len(rows)} {noun}(s), {'wrote' if wrote else 'unchanged'} {out}")
    return 0
