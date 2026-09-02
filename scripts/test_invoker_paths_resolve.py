"""Guard 1: every `scripts/*.py`/`scripts/*.sh` path a real invoker names must exist.

The repo reorganised `scripts/` into subdirectories (#443), then #447 fixed "49 script paths
the subdirectory sweep missed", then #450 split `probe.py` into sibling modules, then #451
cleaned up more stragglers. Four PRs chasing the same class of bug: a cron, a systemd unit, a
prek hook `entry:`, a CI step, or a `.claude/hooks` wrapper that names a script by its OLD path
only fails at RUNTIME -- nothing in the existing test suite walks these invocation sites.

`pyproject.toml`'s `pythonpath` list is a pytest-only mechanism (see the repo-root CLAUDE.md,
the block near line 29): it lets pytest itself resolve a cross-directory import, but a cron or
a prek hook invokes the script directly, with no pytest involved, so a stale path there raises
nothing pytest can see. This test reads the same invocation sites a human would check by hand
after a rename -- prek's `entry =`, a GitHub Actions `run:` step, an `ansible.builtin.cron`
`job:`, the shell templates crons wrap, a systemd unit's `ExecStart*=`, a `.claude/hooks`
wrapper, and `.claude/settings*.json`'s permission strings -- and asserts every `scripts/...`
token found there names a file that exists on disk.

File discovery and field extraction (WHICH files, WHICH field) live in
`lib/invocation_sites.py`, shared with `scripts/docs/gen_reference_scripts.py` -- the other
place that walks these same sites, to say HOW a script is run rather than whether it exists.
What stays local to this file is the token regex below and the comment-line filtering: this
test deliberately takes a conservative superset for the shell categories (the `initial_setup`
`*.sh.j2` templates and the `.claude/hooks` wrappers), where the generator excludes comment
lines, backtick prose and `echo` lines. Extraction is scoped to the exact field each mechanism
executes, not a blind grep for the substring `scripts/` over whole files. That substring also
shows up in comments and docstrings that execute nothing, and in `docs/archive/` (deliberately
describing pre-reorg paths) and `scripts/docs/test_mkdocs_repo_links.py:137` (a NEGATIVE
fixture proving a stale path is not re-linked). Neither is one of the invocation sites this
test reads, so neither needs excluding here -- but if this test is ever extended to a new
source, re-check that both stay out of scope rather than relying on a regex to skip them.

For the two shell-syntax categories (the `initial_setup` `*.sh.j2` templates and the
`.claude/hooks` `*.sh`/`*.py` wrappers) a whole-line `#` comment is skipped; anything else on a
non-comment line is scanned, including a `scripts/...` mention embedded in a string argument
(e.g. a `gh pr create --body` message) -- a conservative superset, not a gap, since a stale
mention there is still worth catching even though it isn't strictly "invoking" anything.
"""

from __future__ import annotations

import re
from pathlib import Path

from lib.invocation_sites import (
    claude_hook_files,
    claude_settings_entries,
    cron_jobs as _shared_cron_jobs,
    prek_hook_entries,
    sh_j2_templates as _shared_sh_j2_templates,
    systemd_exec_lines as _shared_systemd_exec_lines,
    workflow_run_steps as _shared_workflow_run_steps,
)

REPO = Path(__file__).resolve().parent.parent

_TOKEN_RE = re.compile(r"scripts/[\w./-]+\.(?:py|sh)")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def _non_comment_lines(path: Path) -> list[tuple[int, str]]:
    """Lines of a shell/python-syntax file, skipping ones that are wholly a `#` comment."""
    out = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        if line.strip().startswith("#"):
            continue
        out.append((lineno, line))
    return out


def prek_entries() -> list[tuple[str, str]]:
    """Every `scripts/...` token in a prek.toml hook's `entry =`."""
    return [
        (loc, token)
        for loc, entry in prek_hook_entries(REPO)
        for token in _tokens(entry)
    ]


def workflow_run_steps() -> list[tuple[str, str]]:
    """Every `scripts/...` token in a `run:` step of a GitHub Actions workflow."""
    return [
        (loc, token)
        for loc, run in _shared_workflow_run_steps(REPO)
        for token in _tokens(run)
    ]


def cron_jobs() -> list[tuple[str, str]]:
    """Every `scripts/...` token in an `ansible.builtin.cron` task's `job:`, tree-wide.

    Not scoped to `initial_setup` -- traefik, claude-otel, qbittorrent and eight other
    roles each carry their own cron tasks (`lib/invocation_sites.py` finds all of them).
    """
    return [
        (f"{job.path.relative_to(REPO)} task {job.name!r} job=", token)
        for job in _shared_cron_jobs(REPO)
        for token in _tokens(job.job)
    ]


def sh_j2_templates() -> list[tuple[str, str]]:
    """Every `scripts/...` token on a non-comment line of an initial_setup `*.sh.j2`."""
    out = []
    for tpl in _shared_sh_j2_templates(REPO):
        if tpl.parent.parent.name != "initial_setup":
            continue
        for lineno, line in _non_comment_lines(tpl):
            for token in _tokens(line):
                out.append((f"{tpl.relative_to(REPO)}:{lineno}", token))
    return out


def systemd_exec_lines() -> list[tuple[str, str]]:
    """Every `scripts/...` token on an `ExecStart*=` line of a systemd `*.service.j2`."""
    return [
        (loc, token)
        for loc, line in _shared_systemd_exec_lines(REPO)
        for token in _tokens(line)
    ]


def claude_hook_wrappers() -> list[tuple[str, str]]:
    """Every `scripts/...` token on a non-comment line of a `.claude/hooks` wrapper.

    Excludes `test_*` files there -- those are tests of the hooks (fixture command strings
    fed to a classifier), not invocation sites themselves.
    """
    out = []
    for f in claude_hook_files(REPO):
        for lineno, line in _non_comment_lines(f):
            for token in _tokens(line):
                out.append((f"{f.relative_to(REPO)}:{lineno}", token))
    return out


def claude_settings() -> list[tuple[str, str]]:
    """Every `scripts/...` token in a `.claude/settings*.json` permission string."""
    return [
        (loc, token)
        for loc, entry in claude_settings_entries(REPO)
        for token in _tokens(entry)
    ]


_SOURCES = {
    "prek.toml entry=": prek_entries,
    "workflow run: steps": workflow_run_steps,
    "crons.yml job:": cron_jobs,
    "initial_setup *.sh.j2": sh_j2_templates,
    "systemd ExecStart*=": systemd_exec_lines,
    ".claude/hooks wrappers": claude_hook_wrappers,
    ".claude/settings*.json": claude_settings,
}


def all_invocations() -> list[tuple[str, str]]:
    return [hit for fn in _SOURCES.values() for hit in fn()]


def test_every_invoked_script_path_resolves():
    missing = [
        (loc, token) for loc, token in all_invocations() if not (REPO / token).is_file()
    ]
    assert not missing, (
        "invoker names a scripts/ path that does not exist on disk:\n"
        + "\n".join(f"  {loc}: {token}" for loc, token in missing)
    )


# Verified by hand (2026-08-27): no `*.service.j2` unit currently execs a `scripts/...` path --
# every ExecStart that reaches this repo's tooling runs a copy already staged to /opt (e.g.
# gitops-deploy.service.j2 execs /opt/gitops-deploy/gitops_deploy.py, not the scripts/ source
# tree). Exempted here rather than deleted: the extractor stays live for the day a unit does
# call into scripts/ directly, and that day it must be caught, not silently zero forever.
_EXPECTED_EMPTY_SOURCES = {"systemd ExecStart*="}


def test_each_source_is_reachable():
    """A parser regression that silently returns [] for one source would make the assertion
    above vacuous for that source. Every category except the documented exemption above
    currently names at least one real scripts/ path -- pin that so a broken extractor (e.g.
    crons.yml failing to parse as YAML and being swallowed) is caught here instead of by the
    guard going quiet."""
    counts = {name: len(fn()) for name, fn in _SOURCES.items()}
    empty = [
        name
        for name, count in counts.items()
        if count == 0 and name not in _EXPECTED_EMPTY_SOURCES
    ]
    assert not empty, (
        f"these sources found zero scripts/ tokens (extractor regression?): {empty}. Counts: {counts}"
    )


def test_the_scan_is_not_empty():
    total = all_invocations()
    assert len(total) >= 15, total


# --- composed paths -------------------------------------------------------------------
# The literal-token scan above cannot see a path a shell BUILDS at runtime. That is not
# hypothetical: `.claude/hooks/validate-compose.sh` ran `"$repo_root/scripts/${script}.py"`
# from #443 until 2026-08-27, invoking three validators at a path none of them had lived at
# for weeks, and it stayed invisible because there is no literal `scripts/<name>.py` token to
# resolve. This is the recorded "textual guard checks break on an indirection" shape, sitting
# inside the guard written to catch this very class -- so the composed form gets its own arm.
#
# Resolution is deliberately narrow: one shell idiom (a `for VAR in "a:b" ...` list feeding a
# `${VAR#*:}` / `${VAR%%:*}` split). Anything it cannot resolve FAILS as unresolvable rather
# than being skipped, because a composed path this cannot read is exactly the case that hid
# the bug. Widen the resolver when a new idiom appears; never widen the skip.
_COMPOSED = re.compile(
    r"(scripts/[A-Za-z0-9_./-]*)\$\{(\w+)(?:[#%][^}]*)?\}([A-Za-z0-9_./-]*)"
)
_FOR_LIST = re.compile(r"^\s*for\s+(\w+)\s+in\s+(.+?)(?:;|\s*\\?\s*)$")
_SPLIT_ASSIGN = re.compile(r"""(\w+)="\$\{(\w+)([#%]{1,2})([^}]*)\}\"""")


def _candidate_values(path: Path, var: str) -> list[str] | None:
    """Possible literal values of `var` in `path`, or None if the idiom isn't understood."""
    text = path.read_text()
    src, op = None, None
    for name, base, operator, _pat in _SPLIT_ASSIGN.findall(text):
        if name == var:
            src, op = base, operator
            break
    if src is None:
        return None
    # Join backslash-continuations into logical lines FIRST: a `for` list routinely spans
    # several physical lines, and sweeping forward "until the quotes stop" instead swallows
    # every quoted string in the rest of the file.
    logical: list[str] = []
    buf = ""
    for _lineno, line in _non_comment_lines(path):
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
            continue
        logical.append(buf + stripped)
        buf = ""
    if buf:
        logical.append(buf)

    words: list[str] = []
    for line in logical:
        m = _FOR_LIST.match(line)
        if m and m.group(1) == src:
            words += re.findall(r'"([^"]+)"', m.group(2))
            break
    if not words:
        return None
    out = []
    for w in words:
        if op.startswith("#"):
            out.append(w.split(":", 1)[1] if ":" in w else w)
        else:
            out.append(w.split(":", 1)[0])
    return out


def composed_hook_invocations() -> list[tuple[str, str | None]]:
    """(location, resolved path or None-if-unresolvable) for composed `scripts/...` paths."""
    out: list[tuple[str, str | None]] = []
    hooks_dir = REPO / ".claude/hooks"
    for f in sorted(hooks_dir.glob("*.sh")):
        if f.name.startswith("test_"):
            continue
        for lineno, line in _non_comment_lines(f):
            for prefix, var, suffix in _COMPOSED.findall(line):
                loc = f"{f.relative_to(REPO)}:{lineno}"
                values = _candidate_values(f, var)
                if values is None:
                    out.append((f"{loc} (${{{var}}} unresolvable)", None))
                    continue
                for v in values:
                    out.append((loc, f"{prefix}{v}{suffix}"))
    return out


def test_every_composed_script_path_resolves():
    hits = composed_hook_invocations()
    broken = [
        (loc, token)
        for loc, token in hits
        if token is None or not (REPO / token).is_file()
    ]
    assert not broken, (
        "a hook builds a scripts/ path that does not exist (or that this test cannot "
        "resolve -- see the note above; an unresolvable composition is a failure, not a "
        "skip):\n" + "\n".join(f"  {loc}: {token}" for loc, token in broken)
    )


def test_the_composed_scan_still_finds_its_known_site():
    """validate-compose.sh is the only composed-path site today. If it stops matching -- the
    hook is rewritten, the regex drifts -- this arm would silently assert nothing, so pin it."""
    locs = {loc.split(":")[0] for loc, _ in composed_hook_invocations()}
    assert ".claude/hooks/validate-compose.sh" in locs, locs
