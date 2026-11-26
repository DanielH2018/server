#!/usr/bin/env python3
"""Whether a clone's HEAD is a trustworthy anchor for `bootstrap-host.sh --local-build`.

C5's anchor rule, as ADR-0021 states it for the builder: the commit is on origin/master's
first-parent chain and its CI verdict is green. The script runs this from the archived tree
under `uv run --frozen`, because it reads the required CI contexts from the tree's
`estate/vars.yml`. It reads the clone and writes nothing under its working tree: the fetch
touches `.git` only.

Usage: bootstrap_anchor.py <clone> <sha> --repo OWNER/NAME
`--help` prints this text and exits 0.
Exit codes: 0 anchored; 1 refused, naming the half (`first-parent` or `ci`); 64 usage.
"""

import json
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import yaml  # noqa: E402
from builder_ci_grade import read_check_pages  # noqa: E402
from ci_verdict import CheckRun, CIVerdict, ci_verdict, parse_check_runs  # noqa: E402


def master_chain(clone: Path, repo: str) -> list[str]:
    """origin/master's first-parent chain, read through a fetch that writes only .git."""
    url = f"https://github.com/{repo}.git"
    subprocess.run(
        ["git", "-C", str(clone), "fetch", "--quiet", url, "master"], check=True
    )
    out = subprocess.run(
        ["git", "-C", str(clone), "rev-list", "--first-parent", "FETCH_HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.split()


def check_runs(
    repo: str, sha: str, opener: Callable = urllib.request.urlopen
) -> list[CheckRun]:
    """Every check-run of `sha`, across pages. A short read raises, which reads as no verdict."""

    def fetch_page(query: str) -> object:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/commits/{sha}/check-runs{query}",
            headers={
                "User-Agent": "bootstrap-host",
                "Accept": "application/vnd.github+json",
            },
        )
        with opener(request, timeout=30) as response:
            return json.load(response)

    return parse_check_runs(read_check_pages(fetch_page, "check_runs"))


def required_contexts(tree: Path) -> frozenset[str]:
    """`ci_required_contexts` from the tree the script archived."""
    variables = yaml.safe_load((tree / "estate/vars.yml").read_text())
    return frozenset(variables["ci_required_contexts"])


def judge(
    sha: str,
    chain: list[str],
    runs: Callable[[], list[CheckRun]],
    contexts: frozenset[str],
) -> str | None:
    """None when `sha` anchors; otherwise the refusal, naming the half that failed."""
    if sha not in chain:
        return f"first-parent: {sha} is not on origin/master's first-parent chain"
    if not contexts:
        return "ci: ci_required_contexts is empty, so every commit would grade green"
    try:
        verdict = ci_verdict(runs(), contexts)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return f"ci: the verdict of {sha} is unreadable: {exc}"
    if verdict is not CIVerdict.PASS:
        return f"ci: the verdict of {sha} is {verdict}"
    return None


def main(argv: list[str]) -> int:
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0
    if len(argv) != 4 or argv[2] != "--repo":
        print(__doc__, file=sys.stderr)
        return 64
    clone, sha, repo = Path(argv[0]), argv[1], argv[3]
    tree = HERE.parents[1]
    problem = judge(
        sha,
        master_chain(clone, repo),
        lambda: check_runs(repo, sha),
        required_contexts(tree),
    )
    if problem:
        print(f"bootstrap-host: refused, {problem}", file=sys.stderr)
        return 1
    print(
        f"bootstrap-host: {sha[:12]} is on master's first-parent chain with a green CI verdict"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
