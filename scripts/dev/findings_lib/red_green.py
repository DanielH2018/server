"""Which findings `open` labels `red-green`, so a `--review` fan-out gives them a red phase.

WHY (#3950). The red phase runs only for a batch whose every issue carries the label, and
nothing applied it: the phase ran in 0 of 68 review batches. A finding qualifies when every
path it cites lies in code this repo's suite runs, because the red gate proves the new tests
fail by running them. The operator can still add or remove the label by hand.
"""

from pathlib import Path, PurePosixPath

RED_GREEN_LABEL = "red-green"
# `plan_ensure_label` creates it the first time `open` applies it, as it does a dated label.
RED_GREEN_STYLE = (
    "a6e3a1",
    "A --review fan-out writes failing tests for this before the fix (red phase)",
)
REPO = Path(__file__).resolve().parents[3]


def suite_covered(path: str, repo: Path = REPO) -> bool:
    """Whether `path` is code the suite runs, or a test it collects.

    Python under `scripts/`, the filter plugins and the hooks, and anything a role ships from
    `files/` when that role has its own `tests/`: monitor-bridge's registry and the HA Jinja
    macros are both that shape. A doc, a template or a task file is not, because no test
    imports it.
    """
    p = PurePosixPath(path)
    if p.name.startswith("test_") and p.suffix == ".py":
        return True
    if p.parts[:1] == ("scripts",) and p.suffix == ".py":
        return True
    if p.parent.as_posix() in ("ansible/filter_plugins", ".claude/hooks"):
        return p.suffix == ".py"
    if (
        p.parts[:2] == ("ansible", "roles")
        and len(p.parts) > 5
        and p.parts[4] == "files"
    ):
        return (repo / Path(*p.parts[:4]) / "tests").is_dir()
    return False


def red_green_eligible(paths: list[str], repo: Path = REPO) -> bool:
    """Whether a finding citing `paths` gets the label: it cites some, all suite-covered."""
    return bool(paths) and all(suite_covered(p, repo) for p in paths)
