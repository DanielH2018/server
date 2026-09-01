"""The unattended crons that commit must publish through a PR, and must say why they failed.

Both properties were learned on 2026-08-25. docs-refresh.sh and secret-rotate.sh each ended
in a direct write to the default branch, which a repository ruleset rejects outright
("Required status check ... is expected"), so neither had ever published. And both sent the
failure to /dev/null, so the alert said it failed and nothing said why -- the cause stayed
invisible until the rejected command was run by hand.

secret-rotate is the more consequential of the two: it changes a live credential and
redeploys its consumer BEFORE publishing, so an unpublished rotation leaves the running value
and origin disagreeing, and the next deploy from origin re-applies the superseded one.

These are text assertions over the templates, which is the weaker kind of guard -- an
indirection through a variable or a helper would slip past. They are still worth having,
because the regression they cover is someone reinstating a direct write, and that is a
literal line of shell.

The second half of this file is a different invariant on the same crons: no Kuma push token
reaches curl's argv. It carries its own header above `ROLES`, including why its corpus is
derived rather than listed.
"""

import re
from pathlib import Path

import pytest

from _helpers import REPO

TEMPLATES = REPO / "ansible/roles/setup/initial_setup/templates"

DOCS_REFRESH = TEMPLATES / "docs-refresh.sh.j2"
SECRET_ROTATE = TEMPLATES / "secret-rotate.sh.j2"
ROTATION_AUDIT = TEMPLATES / "secret-rotation-audit.sh.j2"

# (path, the commit invocation as it appears in that script). secret-rotate splits its commit
# across lines and passes --no-verify, so a single shared marker would silently match neither.
SCRIPTS = [
    pytest.param(DOCS_REFRESH, 'git commit -m "docs:', id="docs-refresh"),
    pytest.param(SECRET_ROTATE, "commit --no-verify -m", id="secret-rotate"),
]


def read(path: Path) -> str:
    text = path.read_text()
    assert len(text) > 2000, f"{path.name} is only {len(text)} bytes — has it moved?"
    return text


def code_lines(path: Path) -> list[str]:
    """Shell lines only. Both headers document the old direct write at length, and a comment
    quoting `git push` must not be mistaken for one."""
    return [
        line
        for line in read(path).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


@pytest.mark.parametrize(("path", "commit_marker"), SCRIPTS)
def test_it_opens_a_pull_request(path, commit_marker):
    text = read(path)
    assert "gh pr create" in text, (
        f"{path.name} opens no PR; a direct write is rejected"
    )
    assert "gh pr merge --auto" in text, (
        f"{path.name} opens a PR but never lands it — without --auto the cron needs a human, "
        f"and the in-flight guard then blocks every subsequent run"
    )


@pytest.mark.parametrize(("path", "commit_marker"), SCRIPTS)
def test_nothing_is_written_to_the_default_branch(path, commit_marker):
    """The bug. Every push must name the run's branch."""
    pushes = [line for line in code_lines(path) if re.search(r"\bgit push\b", line)]
    assert pushes, f"{path.name} has no git push — the branch never reaches the remote"
    for line in pushes:
        assert '"$BRANCH"' in line, (
            f"{path.name} pushes somewhere other than the run's branch, which the repository "
            f"ruleset rejects: {line.strip()!r}"
        )


@pytest.mark.parametrize(("path", "commit_marker"), SCRIPTS)
def test_failure_paths_keep_their_error_text(path, commit_marker):
    """A failure whose reason goes to /dev/null cost a day of diagnosis."""
    for command in (commit_marker, "git push -u", "gh pr create", "gh pr merge --auto"):
        lines = [line for line in code_lines(path) if command in line]
        assert lines, f"{command!r} not found in {path.name}"
        for line in lines:
            assert "/dev/null" not in line, (
                f"{path.name} discards the error from {command!r}; route it to the log file "
                f"so the alert can say why: {line.strip()!r}"
            )


@pytest.mark.parametrize(("path", "commit_marker"), SCRIPTS)
def test_the_checkout_is_not_left_ahead_of_origin(path, commit_marker):
    """Leaving the commit on the local branch breaks gitops-deploy's --ff-only once the squash
    lands under a new SHA. That parked the deployer behind origin twice during diagnosis."""
    assert "git reset --hard HEAD~1" in read(path), (
        f"{path.name} keeps the commit locally after publishing the branch, so the next "
        f"fast-forward fails and the deployer parks"
    )


def test_secret_rotate_refuses_to_stack_an_unlanded_rotation():
    """Rotating again while a previous rotation is unpublished moves the live credential a
    second time while the first is still unrecorded. It must refuse, not skip quietly.

    The gate is the REMOTE BRANCH, not an open PR. `gh pr create` runs after `git push`, so a
    create failure leaves the branch on origin with no PR at all — and the publish block's
    `git reset --hard HEAD~1` then erases every local trace. An open-PR check passes cleanly
    in exactly the state that most needs to refuse (2026-08-25 review H-1).
    """
    text = read(SECRET_ROTATE)
    assert "git ls-remote --heads origin" in text, (
        "no remote-branch check; a rotation whose `gh pr create` failed leaves no local "
        "evidence, so a local-only guard cannot see it and the next run stacks on it"
    )
    guard = text.split("git ls-remote --heads origin", 1)[1]
    assert "exit 1" in guard, (
        "the in-flight branch must exit non-zero — a silent skip hides that the live value "
        "and origin disagree"
    )


def test_secret_rotate_fails_closed_when_origin_is_unreachable():
    """`git ls-remote ... || true` would read an unreachable origin as "no stale branch" and
    rotate straight into the state the guard exists to refuse. The exit status must be tested.
    """
    text = read(SECRET_ROTATE)
    line = next(
        line for line in code_lines(SECRET_ROTATE) if "git ls-remote --heads" in line
    )
    assert "|| true" not in line, (
        f"the remote-branch check swallows its exit status, so an unreachable origin fails "
        f"OPEN rather than closed: {line!r}"
    )
    assert line.lstrip().startswith("if !"), (
        f"the remote-branch check must branch on its exit status: {line!r}"
    )
    assert "cannot reach origin" in text, (
        "no distinct alert for an unreachable origin; it would be indistinguishable from a "
        "clean run"
    )


def test_secret_rotate_never_reverts_a_live_rotation():
    """The credential is already live and its consumer redeployed by the time this publishes.

    Reverting there would discard the only record of the value that is actually running.
    """
    lines = code_lines(SECRET_ROTATE)
    starts = [i for i, line in enumerate(lines) if "commit --no-verify -m" in line]
    assert len(starts) == 1, f"expected one commit invocation, found {len(starts)}"
    # Comments only, deliberately excluded: the header and the publish block both discuss
    # reverting in order to rule it out, and matching prose would fail on the explanation.
    after = [
        line for line in lines[starts[0] :] if re.search(r"(^|\s|;)revert\b", line)
    ]
    assert not after, (
        f"the publish path calls revert after the rotation is live, which strands the "
        f"running value: {after!r}"
    )


def test_the_audit_watches_for_an_unlanded_rotation_branch():
    """The weekly gate refuses to stack, but nothing would REPORT the stuck state between
    Sundays. The daily audit is the sticky signal, and its two existing arms read only local
    state -- a clean tree and an unrotated registry are exactly what the failure looks like.
    """
    text = ROTATION_AUDIT.read_text()
    assert "git ls-remote --heads origin" in text, (
        "the daily audit cannot see an unlanded rotation branch, so a failed `gh pr create` "
        "goes unreported until the next Sunday's gate happens to trip"
    )


# --- No Kuma push token reaches curl's argv -------------------------------------------------
#
# /proc is mounted without hidepid on all three hosts, so any local user can read another user's
# /proc/<pid>/cmdline. A Kuma push token in there is enough to forge or suppress a heartbeat,
# which hides a real outage. kuma-push-lib.sh:26-30 states the threat model; the fix is to feed
# the token-bearing URL to curl on stdin as a config file (`-K -`) instead of as an argument.
#
# WHY THE CORPUS IS DERIVED, NOT LISTED. The predecessor of these tests iterated
# `(SECRET_ROTATE, ROTATION_AUDIT)` -- exactly the two files its own PR fixed -- so it read green
# while six other templates leaked. That is the estate's most durable failure mode (a guard
# written alongside its fix inherits the fix's scope) at run 5. Two consequences:
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

# Sources /usr/local/lib/kuma-push-lib.sh, with the reason each exemption is honest.
_LIBRARY_EXEMPT = {
    "crowdsec-update-home-allowlist.sh.j2": (
        "keeps its own curl for the `--retry 3 --retry-all-errors` its # DECIDED: marker "
        "records. Those retries are measured against this monitor specifically (push-type, "
        "max_retries 0); kuma_push has ten other callers and none of them measured it. It "
        "reproduces the -K stdin form inline, so the argv assertion still binds it."
    ),
    "renovate-notify.service.j2": (
        "a systemd ExecStartPost running /bin/sh, which is dash on Ubuntu. kuma-push-lib.sh is "
        "bash-shaped, so sourcing it would trade an argv leak for a shell-dialect dependency "
        "inside a unit file. It uses the -K stdin form inline instead."
    ),
}

# Files whose disappearance from the corpus means the derivation broke, not that the estate
# changed. Every one is a template that can invoke curl against a push URL.
_EXPECTED_IN_CORPUS = {
    "ansible/roles/k8s/claude-otel/templates/telemetry-health.sh.j2",
    "ansible/roles/k8s/configarr/templates/configarr-health.sh.j2",
    "ansible/roles/k8s/crowdsec/templates/crowdsec-appsec-verify.sh.j2",
    "ansible/roles/k8s/crowdsec/templates/crowdsec-update-home-allowlist.sh.j2",
    "ansible/roles/k8s/janitorr/templates/janitorr-health.sh.j2",
    "ansible/roles/k8s/registry/templates/registry-gc.sh.j2",
    "ansible/roles/k8s/traefik/templates/cloudflare-ip-drift.sh.j2",
    "ansible/roles/setup/fake_remux/templates/fake-remux-health.sh.j2",
    "ansible/roles/setup/initial_setup/templates/secret-rotate.sh.j2",
    "ansible/roles/setup/initial_setup/templates/secret-rotation-audit.sh.j2",
    "ansible/roles/setup/k3s/templates/disk-health.sh.j2",
    "ansible/roles/setup/k3s/templates/etcd-snapshot-offbox.sh.j2",
    "ansible/roles/setup/k3s/templates/longhorn-backup-health.sh.j2",
    "ansible/roles/setup/k3s/templates/manifest-prune-check.sh.j2",
    "ansible/roles/setup/k3s/templates/remember-logs-health.sh.j2",
    "ansible/roles/setup/optimize_pi/templates/pi-recovery-health.sh.j2",
    "ansible/roles/setup/optimize_pi/templates/pi-sd-health.sh.j2",
    "ansible/roles/setup/renovate_notify/templates/renovate-notify.service.j2",
}

# The corpus also holds six templates that carry a push URL but invoke no curl of their own: the
# authelia bypass regex, both cloudflare-ddns Deployments, both pi-peer-backup manifests, and
# renovate-notify's config.env. They are counted rather than named, so the floor is what watches
# them. 23 against a measured 24 leaves headroom for exactly one retirement — a slacker floor
# would let a third of that unnamed tail vanish before anything went red.
_MIN_CORPUS = 23


def push_corpus() -> list[Path]:
    """Every non-archive template that mentions a push URL, whatever its extension.

    `ansible/roles/containers/archive/` is excluded: those roles deploy nothing, and
    docker-fleet-health.sh.j2 there still carries the pre-library form.
    """
    return sorted(
        path
        for path in ROLES.rglob("*.j2")
        if "/archive/" not in str(path)
        and (PUSH_LITERAL in path.read_text() or "PUSH_URL" in path.read_text())
    )


def _logical_lines(text: str) -> list[str]:
    r"""Shell lines with backslash continuations joined, comments dropped.

    The join is the whole point. In fake-remux-health.sh.j2 the `curl` sat on one physical line
    and the token-bearing URL five lines below it, so a line-local predicate matched neither --
    the naive widening of this guard would have landed green and inert (2026-08-27 review H-2).
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
    """The security property. No exemptions -- an exempt file is one that may write its own
    curl, not one that may leak the token."""
    offenders = {}
    for path in push_corpus():
        leaks = curl_lines_leaking_a_push_url(path.read_text())
        if leaks:
            offenders[str(path.relative_to(REPO))] = leaks
    assert not offenders, (
        "these templates pass a token-bearing push URL to curl as an argv element, which "
        "exposes it in /proc/<pid>/cmdline for the life of the call. Feed it in on stdin "
        'instead -- `printf \'url = "%s"\\n\' "$URL" | curl -G -K - ...`, as '
        f"kuma-push-lib.sh:41-46 does: {offenders}"
    )


def test_every_shell_push_script_sources_the_shared_library():
    """crons.yml:15-19 asserts this in prose. Two files are honestly exempt; both are named
    with their reason, and both remain bound by the argv assertion above."""
    offenders = []
    for path in push_corpus():
        if path.name in _LIBRARY_EXEMPT or not path.name.endswith(".sh.j2"):
            continue
        if "kuma-push-lib.sh" not in path.read_text():
            offenders.append(str(path.relative_to(REPO)))
    assert not offenders, (
        "these push scripts do not source the shared library, contradicting crons.yml:15-19. "
        "Either source it or add a named exemption to _LIBRARY_EXEMPT with the reason: "
        + ", ".join(offenders)
    )


def test_the_library_exemptions_still_name_live_files():
    """An exemption for a file that no longer exists silently widens the next one."""
    names = {path.name for path in push_corpus()}
    stale = set(_LIBRARY_EXEMPT) - names
    assert not stale, (
        "_LIBRARY_EXEMPT names templates that are no longer in the push corpus: "
        + ", ".join(sorted(stale))
    )


def test_the_push_corpus_never_shrinks():
    """The point of the whole rewrite. A guard whose corpus quietly narrows is worse than no
    guard, because it reads green while the class it covers spreads."""
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
    """Both publishing crons authenticate with one `gh` OAuth token that is not in
    secrets.yml and not in the rotation registry, so nothing watched it. Revoked, they both
    keep running, both fail at `gh pr create`, and both stop publishing silently
    (2026-08-25 review M-6).
    """
    text = ROTATION_AUDIT.read_text()
    assert "gh auth status" in text, (
        "nothing checks the credential secret-rotate and docs-refresh publish with"
    )
    line = next(
        line
        for line in text.splitlines()
        if "gh auth status" in line and not line.strip().startswith("#")
    )
    assert "if !" in line, (
        f"the gh check must branch on its exit status, not on its output: {line!r}"
    )


def test_the_audit_grace_is_shorter_than_the_gap_to_the_first_audit():
    """The grace period must not swallow the first audit after a failed publish.

    rotate is Sunday 09:00 and the audit is daily 08:00 (crons.yml), so that first audit is
    23h later. A 24h grace let it push UP -- green on the operator's Monday morning, with the
    sticky DOWN not appearing until Tuesday. That is M-1 narrowed to a day, not closed.
    """
    crons = (TEMPLATES.parent / "tasks/crons.yml").read_text()
    assert 'hour: "8"' in crons and 'hour: "9"' in crons, (
        "the cron schedule moved; re-derive the grace period against the new times"
    )
    text = ROTATION_AUDIT.read_text()
    seconds = [int(m) for m in re.findall(r"-gt (\d{4,})", text)]
    assert seconds, "no age threshold found in the stray-branch arm"
    assert max(seconds) < 23 * 3600, (
        "the grace period is at least as long as the 23h gap between a Sunday 09:00 rotate "
        "and the next 08:00 audit, so the first audit after a failed publish reports UP: %s"
        % seconds
    )


def test_the_audit_branch_arm_is_additive_not_a_short_circuit():
    """The two arms above it exit 0 before the auditor runs, which is right for a registry
    that cannot be trusted. A stray branch says nothing about the OTHER secrets, so
    short-circuiting there would silence every overdue secret until a human cleared it.
    """
    text = ROTATION_AUDIT.read_text()
    arm = text.split("git ls-remote --heads origin", 1)[1]
    # Up to the auditor invocation: the arm must reach it, not exit ahead of it.
    before_audit = arm.split("secret_rotation.py audit", 1)[0]
    assert "exit 0" not in before_audit, (
        "the stray-branch arm short-circuits past the auditor, suppressing overdue "
        "reporting for every other secret while the branch sits there"
    )
    assert "--extra-down" in arm, (
        "the arm does not force the monitor DOWN; the auditor would push UP and mask it"
    )


def test_the_two_audit_arms_accumulate_rather_than_suppress_each_other():
    """A revoked `gh` token is what makes `gh pr create` fail and strand the branch, so the
    two faults are causally linked and the compound case is the one H-1 exists for. Gating the
    branch arm on the token arm reports the cause and hides the consequence -- a live rotated
    credential, unpublished on origin.

    `git ls-remote` authenticates over git's own credential path, not through `gh`, so the
    branch arm still works with a dead token and has no reason to be skipped.
    """
    text = ROTATION_AUDIT.read_text()
    lines = [line for line in code_lines(ROTATION_AUDIT) if "EXTRA_DOWN" in line]

    # The branch arm's condition must not read EXTRA_DOWN -- that is the short-circuit.
    branch_arm = text.split("# Runs even when the arm above fired", 1)
    assert len(branch_arm) == 2, "the stray-branch arm lost its ordering comment"
    # "; then", not "then": the prose above the arm contains "au-then-ticates", and splitting
    # on the bare word truncated ahead of the condition -- the guard passed on the mutation it
    # exists to catch.
    condition = branch_arm[1].split("; then", 1)[0]
    assert "EXTRA_DOWN" not in condition, (
        "the stray-branch arm is gated on the gh-token arm, so a revoked token silences the "
        f"unpublished-credential report it causes: {condition!r}"
    )

    # Both arms must go through the accumulator, and the accumulator must concatenate.
    assert text.count("add_down ") >= 2, (
        "an arm assigns EXTRA_DOWN directly instead of accumulating, so a second fault "
        "overwrites the first"
    )
    concat = [line for line in lines if 'EXTRA_DOWN="$EXTRA_DOWN' in line]
    assert concat, (
        "add_down replaces rather than appends; the compound fault reports one reason: "
        f"{lines!r}"
    )
