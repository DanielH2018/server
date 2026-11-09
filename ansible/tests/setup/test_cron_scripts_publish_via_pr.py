"""The unattended crons that commit must publish through a PR, and must say why they failed.

A repository ruleset rejects a direct write to the default branch ("Required status check ...
is expected"), so docs-refresh.sh and eval-run.sh must publish through a PR. A failure
sent to /dev/null leaves an alert that says it failed and nothing that says why, so the cause
stays invisible until the rejected command is run by hand.

The publish sequence itself -- branch, push, reset master, `gh pr create`, `gh pr merge
--auto` -- lives in scripts/deploy_tools/publish_pr.py, which the two crons call, and is
EXECUTED by scripts/deploy_tools/tests/test_publish_pr.py. What stays here is text
assertions over the templates, the weaker kind of guard, kept because the regression they
cover is someone reinstating an inline direct write, and that is a literal line of shell.

The second half is a different invariant on the same crons: no Kuma push token reaches
curl's argv. Its header above `ROLES` says why its corpus is derived rather than listed.
"""

import re
from pathlib import Path

import pytest

from _helpers import REPO
from _shell_render import rendered_or_source_text, rendered_shell_text

TEMPLATES = REPO / "ansible/roles/setup/initial_setup/templates"
COMPONENTS = REPO / "hosts/components"
DOCS_REFRESH = COMPONENTS / "docs-refresh/templates/docs-refresh.sh.j2"
ROTATION_AUDIT = COMPONENTS / "secret-rotation/templates/secret-rotation-audit.sh.j2"
EVAL_RUN = COMPONENTS / "eval-sweep/templates/eval-run.sh.j2"
PUBLISH_PR = REPO / "scripts/deploy_tools/publish_pr.py"

# (path, the commit invocation as it appears in that script).
SCRIPTS = [
    pytest.param(DOCS_REFRESH, 'git commit -m "docs:', id="docs-refresh"),
    pytest.param(EVAL_RUN, 'git commit -m "evals:', id="eval-run"),
]


def read(path: Path) -> str:
    in_component = COMPONENTS in path.parents
    role = path.parent.parent.name if in_component else "initial_setup"
    text = rendered_shell_text(
        "components" if in_component else "setup", role, path.name
    )
    assert len(text) > 2000, f"{path.name} is only {len(text)} bytes — has it moved?"
    return text


def code_lines(path: Path) -> list[str]:
    """Shell lines only.

    Both headers document the direct write at length, and a comment quoting `git push` must not
    be mistaken for one.
    """
    return [
        line
        for line in read(path).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


PUBLISHER = "scripts/deploy_tools/publish_pr.py publish"


def _joined_code(path: Path) -> str:
    """Code lines with backslash continuations joined, so a multi-line call reads as one."""
    return re.sub(r"\\\n\s*", " ", "\n".join(code_lines(path)))


@pytest.mark.parametrize(("path", "commit_marker"), SCRIPTS)
def test_it_publishes_through_the_shared_publisher(path, commit_marker):
    """One implementation of branch-push-reset-PR-auto-merge, executed under test, not one per cron."""
    calls = [line for line in _joined_code(path).splitlines() if PUBLISHER in line]
    assert len(calls) == 1, (
        f"{path.name} calls the publisher {len(calls)} times; expected exactly one, after the commit"
    )
    assert '--prefix "$PR_BRANCH_PREFIX"' in calls[0], (
        f"{path.name} publishes under a branch prefix other than its own, so its open-PR "
        f"guard cannot see the PR it opens: {calls[0].strip()!r}"
    )


@pytest.mark.parametrize(("path", "commit_marker"), SCRIPTS)
def test_nothing_is_written_to_the_default_branch(path, commit_marker):
    """The bug: a direct write, which the ruleset rejects. Only the publisher pushes."""
    inline = [
        line
        for line in code_lines(path)
        if re.search(r"\bgit push\b|\bgh pr create\b|\bgh pr merge\b", line)
    ]
    assert not inline, (
        f"{path.name} pushes or opens a PR inline instead of through {PUBLISHER}: {inline!r}"
    )


@pytest.mark.parametrize(("path", "commit_marker"), SCRIPTS)
def test_failure_paths_keep_their_error_text(path, commit_marker):
    """A failure whose reason goes to /dev/null leaves nothing to diagnose from.

    The commit's output goes to the log file the alert reads; the publisher's one-line message
    is captured, stderr included, and alerted verbatim.
    """
    commit = [line for line in code_lines(path) if commit_marker in line]
    assert commit, f"{commit_marker!r} not found in {path.name}"
    for line in commit:
        assert "/dev/null" not in line, (
            f"{path.name} discards the commit error; route it to the log file so the alert "
            f"can say why: {line.strip()!r}"
        )
    call = next(line for line in _joined_code(path).splitlines() if PUBLISHER in line)
    assert "PUB_MSG=" in call and "2>&1" in call and "/dev/null" not in call, (
        f"{path.name} does not capture the publisher's message and stderr for the alert: "
        f"{call.strip()!r}"
    )
    assert re.search(r'(alert|reachout_down) "[^"]*\$PUB_MSG', read(path)), (
        f"{path.name} never alerts with the publisher's message (`reachout_down` counts)"
    )


def test_the_regressed_suffix_strip_matches_what_the_publisher_prints():
    """eval-run.sh.j2's REGRESSED alert strips a literal suffix off PUB_MSG on the assumption
    that publish_pr.py's happy-path message ends exactly that way.

    ``test_the_happy_path_runs_the_six_steps_in_order`` in test_publish_pr.py pins the
    producer's wording, but nothing pinned the two sides to each other -- change the message
    there and this template silently keeps stripping the stale suffix, so the REGRESSED alert
    reads "PR opened for X with auto-merge; one or more eval cases REGRESSED", wrong and not
    obviously wrong.
    """
    # Anchored on PUBLISH_PUBLISHED specifically: "PR opened for {branch}" is also the prefix of
    # the auto-merge-failed message earlier in the same function, and an unanchored search
    # matches that one first.
    match = re.search(
        r'PUBLISH_PUBLISHED,\s*f"PR opened for \{branch\}(.*?)"', PUBLISH_PR.read_text()
    )
    assert match, (
        "publish_pr.py's happy-path message literal has moved; re-derive the suffix"
    )
    suffix = re.sub(r"\{[^}]*\}", "*", match.group(1))

    template = re.search(r"PUB_MSG%%([^}]*)\}", read(EVAL_RUN))
    assert template, "eval-run.sh.j2 no longer strips a %% suffix off PUB_MSG"
    assert template.group(1) == suffix, (
        f"eval-run.sh.j2 strips {template.group(1)!r} off PUB_MSG but publish_pr.py's "
        f"happy path prints {suffix!r} -- the REGRESSED alert would keep the stale suffix"
    )


# --- Every variable a cron reads is one it can see ------------------------------------------
#
# All three scripts run under `set -uo pipefail`, so reading an unset variable aborts the script
# at that line. Every other check misses it: the templates render, ansible-lint passes, and the
# abort only happens on the branch that reads the variable, such as the DEGRADED-run branch.
#
# A read is satisfied when the same template assigns it, when homelab-check-lib.sh does, or when
# it is in _CRON_ENV below. Forms that survive `set -u` --
# ${VAR:-}, ${VAR-}, ${VAR:+}, ${VAR+}, ${VAR:=}, ${VAR=} -- are not reads. Nothing else is
# exempt: ${VAR#...}, ${VAR//...} and ${#VAR} all abort on an unset VAR just as `$VAR` does.

CHECK_LIB = REPO / "ansible/roles/setup/initial_setup/files/homelab-check-lib.sh"

# Environment the unit supplies, with provenance (test_the_cron_env_allowlist_is_still_exported).
_CRON_ENV = {
    "PATH": "Environment= of homelab-docs-refresh.service",
    "KUBECONFIG": "Environment= of homelab-docs-refresh.service",
    "HOME": "Environment= of homelab-eval-sweep.service",
}

# Templates whose reads are checked, and one name each that the read census MUST contain. A
# census that silently stops matching returns an empty set, and "no unsatisfied reads" is what
# an empty set looks like -- the failure mode CLAUDE.md names as surviving the red-proof pair.
_READ_CENSUS_MUST_FIND = {
    DOCS_REFRESH: frozenset({"PUSH_STATUS", "PUSH_MSG", "PUB_MSG", "UV", "REPO"}),
    EVAL_RUN: frozenset({"PUSH_STATUS", "PUB_MSG", "UNLANDED_RC", "REPO", "TREND_RC"}),
    ROTATION_AUDIT: frozenset({"AUDIT_RC", "EXTRA_DOWN", "UV"}),
}

# `${NAME` plus the operator that follows, when there is one.
_BRACED_READ = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:?[-+=])?")
# `$NAME`. `${` cannot match: `{` is not in the name class.
_BARE_READ = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
# The shell keywords an assignment can hide behind: `if ! VAR="$(...)"`, `while VAR=...`.
_LEADING_KEYWORDS = re.compile(r"^(?:(?:if|elif|while|until|then|else|do)\s+|!\s*)*")
_ASSIGNED = re.compile(
    r"^(?:export\s+|readonly\s+|local\s+(?:-\w+\s+)?|declare\s+(?:-\w+\s+)?)?"
    r"([A-Za-z_][A-Za-z0-9_]*)="
)
_FOR_LOOP = re.compile(r"^for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b")
# `local a b c` / `declare x y` bind names with no `=` at all.
_BARE_DECLARATION = ("local ", "declare ", "readonly ")


def shell_reads(text: str) -> set[str]:
    """Every variable name the script reads in a form that aborts under ``set -u``."""
    names = set()
    for line in _logical_lines(text):
        names.update(
            name for name, operator in _BRACED_READ.findall(line) if not operator
        )
        names.update(_BARE_READ.findall(line))
    return names


def shell_assignments(text: str) -> set[str]:
    """Every variable name the script binds, by assignment, loop variable or declaration."""
    names = set()
    for line in _logical_lines(text):
        for chunk in re.split(r"[;&|]+|\(\(|\)\)", line):
            stripped = _LEADING_KEYWORDS.sub("", chunk.strip())
            if match := _ASSIGNED.match(stripped):
                names.add(match.group(1))
            if loop := _FOR_LOOP.match(stripped):
                names.add(loop.group(1))
            if stripped.startswith(_BARE_DECLARATION):
                names.update(
                    word.split("=")[0]
                    for word in stripped.split()[1:]
                    if not word.startswith("-")
                )
    return names


def unsatisfied_reads(text: str) -> set[str]:
    """Names the script reads that neither it, the check library, nor the cron env supplies."""
    supplied = (
        shell_assignments(text)
        | shell_assignments(CHECK_LIB.read_text())
        | set(_CRON_ENV)
    )
    return shell_reads(text) - supplied


@pytest.mark.parametrize(
    "path",
    [pytest.param(path, id=path.name) for path in _READ_CENSUS_MUST_FIND],
)
def test_every_variable_a_cron_reads_is_one_it_can_see(path):
    """`set -u` turns a leftover read into an abort on the branch that reads it."""
    missing = unsatisfied_reads(read(path))
    assert not missing, (
        f"{path.name} reads {sorted(missing)} under `set -u` without assigning them, sourcing "
        f"them from homelab-check-lib.sh, or their being cron environment named in _CRON_ENV. "
        f"Each one aborts the script at that line the first time its branch is taken."
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        pytest.param(path, names, id=path.name)
        for path, names in _READ_CENSUS_MUST_FIND.items()
    ],
)
def test_the_read_census_still_finds_the_variables_it_is_built_on(path, expected):
    """A census that stops matching returns nothing, and nothing has no unsatisfied reads."""
    found = shell_reads(read(path))
    assert expected <= found, (
        f"the read census no longer sees {sorted(expected - found)} in {path.name}, so it is "
        f"reporting green on a set it cannot see. Check _BRACED_READ / _BARE_READ."
    )


def test_a_read_with_no_assignment_is_flagged():
    """The rejecting half: a read of a variable whose assignment was deleted, minimised."""
    template = 'PUSH_MSG="PR opened for $BRANCH"\n'
    assert unsatisfied_reads(template) == {"BRANCH"}


def test_a_read_the_script_assigns_is_clean():
    template = 'BRANCH="docs-refresh/x"\nPUSH_MSG="PR opened for $BRANCH"\n'
    assert unsatisfied_reads(template) == set()


def test_a_set_u_safe_default_is_not_a_read():
    """`${LOG:-}` is the idiom the traps use for a variable set later or not at all."""
    assert unsatisfied_reads('rm -f "${LOG:-}" "${TMP-}" "${DIR:+x}"\n') == set()


def test_a_substring_expansion_of_an_unset_name_is_still_a_read():
    """Only `:- - :+ + := =` survive `set -u`; `#`, `%%` and `//` all abort."""
    assert unsatisfied_reads('echo "${PUB_MSG%% with auto-merge}"\n') == {"PUB_MSG"}


def test_the_cron_env_allowlist_is_still_exported():
    """An entry whose provenance is gone silently widens the guard for everything else."""
    templates = "".join(read(path) for path in _READ_CENSUS_MUST_FIND)
    units = "".join(p.read_text() for p in sorted(COMPONENTS.glob("*/units/*")))
    stale = [
        name
        for name in _CRON_ENV
        if f"{name}=" not in units and f"export {name}=" not in templates
    ]
    assert not stale, (
        "these _CRON_ENV entries are no longer set by any unit or "
        "exported by a template, so the guard is excusing reads nothing supplies: "
        + ", ".join(stale)
    )


# --- No Kuma push token reaches curl's argv -------------------------------------------------
#
# /proc is mounted without hidepid on all three hosts, so any local user can read another user's
# /proc/<pid>/cmdline. A Kuma push token in there is enough to forge or suppress a heartbeat,
# which hides a real outage. The fix is to feed
# the token-bearing URL to curl on stdin as a config file (`-K -`) instead of as an argument.
#
# WHY THE CORPUS IS DERIVED, NOT LISTED. A guard that iterates only the files its own fix
# touched inherits the fix's scope and reads green while other templates leak. Two
# consequences:
#
#   * the corpus is every non-archive `*.j2` under ansible/ that mentions a push URL, in ANY
#     extension. Keying it on `*.sh.j2` would have missed renovate-notify.service.j2, which is a
#     systemd unit AND holds no `api/push` literal -- its URL arrives from config.env at runtime.
#   * `test_the_push_corpus_never_shrinks` fails if the derivation stops seeing what it sees
#     today, so a guard cannot quietly become inert.
#
# The two assertions are deliberately separate and have DIFFERENT exemption policies. A file may
# be excused from using the shared library; nothing is ever excused from keeping the token out of
# argv. Folding them together would let an exemption launder the security property away.
#
# KNOWN GAP, because a text guard has one. A variable is treated as tainted either when THIS file
# assigns it an `api/push` value or when its name contains PUSH_URL. So a URL assigned in one file
# under a name like KUMA_URL and consumed by curl in another slips through both arms -- the same
# cross-file shape as renovate-notify.service.j2, minus the naming convention that catches it.
# Keep new push URLs named *PUSH_URL and the guard keeps binding them.

ROLES = REPO / "ansible/roles"

PUSH_LITERAL = "api/push"
# `$NAME` or `${NAME}`.
_SHELL_VAR = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
# An assignment, optionally exported: `PUSH_URL=`, `export KUMA_PUSH_URL=`.
_ASSIGNMENT = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=")
# curl's config-file flag. Bounded so `--keepalive` and `-Ku` do not count as a match.
_CURL_CONFIG_FLAG = re.compile(r"(?<![\w-])-K(?![\w-])")

# Files whose disappearance from the corpus means the derivation broke, not that the estate
# changed. M6 batch B9's Cutover-2 took the last two shell push scripts, aide-check.sh.j2 and
# rkhunter-check.sh.j2, out of the corpus, and M6's C-1b took authelia's config-secret with its
# bypass regex. The one template left carries a push URL but invokes no curl of its own:
# loki-homelab's config.alloy.
_EXPECTED_IN_CORPUS = {
    "ansible/roles/k8s/loki-homelab/templates/config/config.alloy.j2",
}

# The floor is the measured count: 1 on 2026-11-04, after C-1b. When a later M6 batch
# shrinks the corpus again, re-measure rather than estimate, because a floor above the real count
# fails master CI.
_MIN_CORPUS = 1


def push_corpus() -> dict[Path, str]:
    """Every role or host-component template that mentions a push URL, any extension."""
    paths = [*ROLES.rglob("*.j2"), *REPO.glob("hosts/components/*/templates/*.j2")]
    texts = {p: rendered_or_source_text(p) for p in sorted(paths)}
    return {p: t for p, t in texts.items() if PUSH_LITERAL in t or "PUSH_URL" in t}


def _logical_lines(text: str) -> list[str]:
    r"""Shell lines with backslash continuations joined, comments dropped.

    The join is the whole point. In fake-remux-health.sh.j2 the `curl` sits on one physical line
    and the token-bearing URL five lines below it, so a line-local predicate matches neither --
    the naive widening of this guard would land green and inert.
    """
    joined = re.sub(r"\\\n\s*", " ", text)
    return [
        line
        for line in joined.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def curl_lines_leaking_a_push_url(text: str) -> list[str]:
    """Logical lines that hand curl a push URL without routing it through a config file.

    A URL is tainted when the line holds the `api/push` literal, references a variable this file
    assigned such a literal to, or references a `*PUSH_URL*` name -- the last arm is what catches
    renovate-notify.service.j2, whose URL is assigned in a different file entirely.
    """
    lines = _logical_lines(text)
    tainted_names = {
        match.group(1)
        for line in lines
        if PUSH_LITERAL in line and (match := _ASSIGNMENT.match(line))
    }
    leaks = []
    for line in lines:
        if "curl" not in line:
            continue
        refs = set(_SHELL_VAR.findall(line))
        tainted = (
            PUSH_LITERAL in line
            or bool(refs & tainted_names)
            or any("PUSH_URL" in ref.upper() for ref in refs)
        )
        if tainted and not _CURL_CONFIG_FLAG.search(line):
            leaks.append(line.strip())
    return leaks


def test_no_push_token_reaches_curls_argv():
    """The security property.

    No exemptions -- an exempt file is one that may write its own curl, not one that may leak the
    token.
    """
    offenders = {}
    for path, text in push_corpus().items():
        leaks = curl_lines_leaking_a_push_url(text)
        if leaks:
            offenders[str(path.relative_to(REPO))] = leaks
    assert not offenders, (
        "these templates pass a token-bearing push URL to curl as an argv element, which "
        "exposes it in /proc/<pid>/cmdline for the life of the call. Feed it in on stdin "
        'instead -- `printf \'url = "%s"\\n\' "$URL" | curl -G -K - ...`, as '
        f"not as an argument: {offenders}"
    )


def test_the_push_corpus_never_shrinks():
    """A guard whose corpus quietly narrows is worse than no guard, because it reads green while the
    class it covers spreads.
    """
    found = {str(path.relative_to(REPO)) for path in push_corpus()}
    missing = _EXPECTED_IN_CORPUS - found
    assert not missing, (
        "these templates left the push corpus. Either the derivation stopped matching them or "
        "they were retired -- confirm which before editing this list: "
        + ", ".join(sorted(missing))
    )
    assert len(found) >= _MIN_CORPUS, (
        f"the push corpus is down to {len(found)} templates, below the {_MIN_CORPUS} floor. A "
        "floor well under the real count cannot tell 'the glob broke' from 'one role was "
        "retired'."
    )


def test_the_audit_watches_the_gh_token_both_crons_depend_on():
    """docs-refresh and eval-run authenticate with one `gh` OAuth token that is not in
    secrets.yml and not in the rotation registry, so nothing else watches it. Revoked, they
    both keep running, both fail at `gh pr create`, and both stop publishing silently."""
    text = read(ROTATION_AUDIT)
    assert "gh auth status" in text, (
        "nothing checks the credential docs-refresh and eval-run publish with"
    )
    line = next(
        line
        for line in text.splitlines()
        if "gh auth status" in line and not line.strip().startswith("#")
    )
    assert "if !" in line, (
        f"the gh check must branch on its exit status, not on its output: {line!r}"
    )
