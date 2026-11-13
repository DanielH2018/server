"""`findings.py open` prints no Project warning against the token the register is filed with (#8237).

The live gh token carries `repo`, `workflow`, `read:org` and `gist`, and no `read:project`, so
gh refuses every Project lookup. `refusing_project` wraps the fakes' `gh` to answer the way the
live one does: an `issue create --project` fails with the message every 2026-11-12 filing
printed, and an `api graphql` board read fails on the missing scope. Every other call reaches
the fakes unchanged, so a fix that stops writing to the board passes, and so does one that
writes somewhere gh can resolve.

Run: uv run pytest scripts/dev/tests/test_findings_open_project_warning.py
"""

import dataclasses
import subprocess

from _findings_fakes import Fakes, build_tools
from dev import findings

_CREATE_REFUSAL = "could not add to project: 'Server Issue Tracking' not found\n"
_SCOPE_REFUSAL = (
    "gh: Your token has not been granted the required scopes to execute this query. "
    "The 'projectsV2' field requires one of the following scopes: ['read:project']\n"
)


def refusing_project(fakes: Fakes):
    """`build_tools(fakes)` whose `gh` refuses Project access the way the live token does."""
    tools, calls = build_tools(fakes)
    inner = tools.gh

    def gh(*argv, **kwargs):
        if list(argv[:2]) == ["issue", "create"] and "--project" in argv:
            calls.gh.append(list(argv))
            raise subprocess.CalledProcessError(1, "gh", stderr=_CREATE_REFUSAL)
        if list(argv[:2]) == ["api", "graphql"] and any(
            "projectsV2" in a for a in argv
        ):
            calls.gh.append(list(argv))
            raise subprocess.CalledProcessError(1, "gh", stderr=_SCOPE_REFUSAL)
        return inner(*argv, **kwargs)

    return dataclasses.replace(tools, gh=gh), calls


def _open_argv(body, *extra):
    return [
        "open",
        "--title",
        "T",
        "--body-file",
        str(body),
        "--severity",
        "low",
        "--kind",
        "gap",
        *extra,
    ]


def _warnings(err: str) -> list[str]:
    return [line for line in err.splitlines() if line.startswith("warning:")]


def test_dry_run_then_real_open_prints_no_project_warning(tmp_path, capsys):
    body = tmp_path / "b.md"
    body.write_text("B")
    tools, _ = refusing_project(Fakes())

    assert findings.main(_open_argv(body, "--dry-run"), tools) == 0
    dry = capsys.readouterr()
    assert findings.main(_open_argv(body), tools) == 0
    real = capsys.readouterr()

    assert "#42 created" in real.out
    assert _warnings(dry.err) == []
    assert _warnings(real.err) == []


def test_open_with_a_not_before_date_prints_no_board_warning(tmp_path, capsys):
    body = tmp_path / "b.md"
    body.write_text("B")
    tools, _ = refusing_project(Fakes())

    assert findings.main(_open_argv(body, "--not-before", "2099-01-15"), tools) == 0
    out = capsys.readouterr()

    assert "#42 created" in out.out
    assert _warnings(out.err) == []
