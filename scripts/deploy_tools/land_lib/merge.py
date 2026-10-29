"""The merge phase: check the PR here, then merge it directly once its CI is green.

--arm-merge exists so an unattended session never issues `gh pr merge` itself: it sits on
the ask list, and auto mode suspends the allow list, so a session with nobody to answer the
prompt times out as a denial.
Idempotent: a MERGED PR is left alone, a CLOSED one dies.

--arm-merge never arms GitHub's auto-merge. Auto-merge merges whatever head is green when CI
finishes, while the direct merge here is pinned to the head await_ci read green and the landing
policy checked. A session may still arm auto-merge itself; --arm-merge on the merged PR is then
a no-op. Until 2026-10-11 the "agent branch fence" (ruleset 24517167) covered master, and
auto-merge never applies a ruleset bypass (github/docs#45265), so an armed PR sat BLOCKED until
a hand merge (#3911). A CHANGES_REQUESTED PR is refused outright, because someone asked for it
to change. `gh pr merge` refuses it at its own pre-flight as well (cli/cli#13388).

--await-merge polls the PR's state until merged, and merges it through the REST endpoint once
await_ci reads the head green, pinned to that head SHA. The master CI gate still refuses that
call until its own checks pass. A refusal is reported and the wait goes on, so a merge GitHub
keeps refusing reaches merge-timeout rather than a merge. --arm-merge without --await-merge
therefore dies: nothing in that run would merge the PR.

A head that bumps a forward-only pin (`k3s_longhorn_version`, `k3s_metallb_version`) merges
only after `pin_gate.gate_before_merge` passes it: on daniel-box, with DEPLOY_HOLD at `none`, no
bump of that pin already in flight, its runbook gates green and `homelab/upgrade-gates` posted
on the head. `pin_gate.py`'s docstring has the order and what each step reads.

The direct merge copies every `Removes-Object:` line on the PR's commits into the squash body
it sends (`squash_body`), so `promote.py decide` finds each one on master whatever the repo's
squash default is.

--arm-merge also refuses a PR whose body carries a closing keyword outside a `Closes #N` line,
before any merge call. A "Filed and not fixed: #N" line closes #N on merge, because GitHub
reads the keyword and not the sentence around it. `stray_closing_refs` owns the
rule.

--arm-merge refuses while the repo is not public, before any merge call. On the free plan GitHub
enforces no ruleset on a private repo, so the direct merge would go through without the CI
gate: 29 PRs did on 2026-09-21 (#3610). `_refuse_private_repo` owns it. It cannot stop an
auto-merge armed by hand while the repo was public from firing after a later flip;
github-ruleset-drift.sh is the cover for that.

`opts.require_author` (from `LAND_REQUIRE_AUTHOR`, which renovate-agent-land@.service sets to
`app/renovate`) makes --arm-merge refuse a PR by anyone else, before any merge call. The
agent's contract says "never a PR by another author", and this is the check; an
interactive session leaves the variable unset and is unaffected. The refusal names the hand-off
rather than the `--any-author` override: only the unattended session ever reads it, and the
operator chose that a `manual —` bump goes to a person.

When a lander unit sets the landing policy (`policy.py`), --arm-merge runs its checks and pins
the REST merge to the head SHA they read through its `sha=` field. --await-merge dies if the
head moves before the merge, so a push after the checks needs a re-run, which checks it again.
The one head move it does not die on is its own branch update, below: it runs the checks
again on the updated head and pins that.

--await-merge re-tests a PR whose base is stale before it merges it (issue #4013). Merge
queues are not offered on a user-owned repo and no ruleset requires an up-to-date branch, so
nothing else re-tests a PR against the master it merges into. 4 of 5 sampled red master runs
followed a PR whose every job was green, and two of them came from a master commit merged
after the PR's base. Once the head reads green, `_master_paths_since` asks GitHub which paths
master changed since the master commit the head's green CI run tested it against (the PR's
merge base when that cannot be read). When any did, `_update_branch` merges master into the
PR branch, and the merge waits for CI on the updated head instead. A landing updates the
branch at most once. It never updates the branch of a head the landing policy admitted on
an approval, which the update would void: a stale approved head is refused with a rebase
remedy instead.
"""

import re
import subprocess

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.exit_codes import CI_DISARMED, CI_GREEN, CI_PENDING, CI_RED
from lib.json_types import JsonObject, as_list, as_object
from lib import removes_object
from deploy_tools import await_ci
from deploy_tools.land_lib import gh_read, pace, pin_gate, policy
from deploy_tools.land_lib.landing import BRANCH, Landing
from deploy_tools.land_lib.outcome import Verdict, disarmed, say
from deploy_tools.land_lib.pr_json import CommitMessage


# GitHub's closing keywords, all three tenses of all three verbs. A keyword anywhere in the
# body, with or without a colon, closes the issue it points at when the PR merges — the
# surrounding words decide nothing, so "not fixed: #N" closes #N exactly as "Fixes #N" does.
_KEYWORD = r"close[sd]?|fix(?:e[sd])?|resolve[sd]?"
_CLOSING_REF = re.compile(rf"\b({_KEYWORD})\b:?\s+#(\d+)", re.IGNORECASE)
# One DELIBERATE closing reference with its trailing punctuation, so a second keyword on the
# same line is read against what is left rather than against the first reference's text.
# `Closes #2428, closes #2429` and `Closes #2412. Closes #2477:` are both ordinary here.
_CANONICAL_REF = re.compile(rf"\b({_KEYWORD})\b:?\s+#\d+[.,;:)\]]*\s*", re.IGNORECASE)
# Markdown that carries no meaning in front of a reference: list and blockquote markers,
# heading hashes, bold/italic runs, and the backticks of a code span — GitHub creates no
# reference at all inside one.
_DECORATION = re.compile(r"^[\s>*_#`~-]+|[\s>*_#`~-]+$")
_SENTENCE_END = re.compile(r"[.!?]$")
# How long a head whose every CI run skipped as a draft waits for a ready run before the PR is
# read for its draft state (#6077). GitHub registered PR #6069's ready run 3s after the flip
# at 02:09:58 on 2026-10-24, and the push-delivery grace await_ci already allows is far wider.
DRAFT_SKIPPED_GRACE_S = await_ci.DROPPED_PUSH_GRACE_S


def _reference_is_deliberate(prefix: str) -> bool:
    """Whether a closing keyword preceded by `prefix` on its line is the intended kind.

    Deliberate means the keyword opens a clause of its own: it starts the line, follows
    another closing reference, or follows a finished sentence. `Filed and not fixed: #N`
    fails all three.
    """
    rest = _DECORATION.sub("", _CANONICAL_REF.sub("", prefix))
    return not rest or _SENTENCE_END.search(rest) is not None


def stray_closing_refs(body: str) -> list[str]:
    """The lines of `body` whose closing keyword is not a deliberate closing reference.

    GitHub reads the `fixed: #N` in a body that says "Filed and not fixed: #N" as a closing
    keyword, and closes the unfixed follow-up on merge, which drops it from
    `findings.py list` until someone reopens it by hand.

    Set membership cannot tell the two apart: the intentional form and the accidental one are
    the same construct, so a closing reference the PR does not intend looks exactly like one it
    does. POSITION is the discriminator — a keyword that opens a clause is the convention every
    PR body here writes, and one buried mid-sentence is the accident. A body naming an unfixed
    follow-up writes it without a keyword in front of the number: `Filed for later: #N`.

    The clause test is what the corpus forces. A keyword-opens-the-LINE rule flags the repo's
    ordinary `Closes #A. Closes #B.` on one line, and a guard that refuses a fifth of all
    landings is one the operator turns off. Flagging only a keyword that opens no clause
    avoids that.

    Known limits, both deliberate. A keyword inside a fenced code block is still flagged:
    over-flagging costs one `gh pr edit` and under-flagging costs a silently closed finding.
    And only the bare `#N` form is matched, not `owner/repo#N` or a full issue URL, which
    GitHub also closes on — no agent or template here writes either.

    A pure function of one string so the branch is testable without gh.

    Args:
        body: the PR body, as `gh pr view --json body` returns it.

    Returns:
        The stripped offending lines, in order, without duplicates.
    """
    found: list[str] = []
    for line in body.splitlines():
        for match in _CLOSING_REF.finditer(line):
            if _reference_is_deliberate(line[: match.start()]):
                continue
            if line.strip() not in found:
                found.append(line.strip())
    return found


def _refuse_stray_closing_refs(ln: Landing, body: str) -> None:
    """Die when the PR body carries a closing reference that is not its own `Closes #N` line.

    Runs beside `_require_author`, before any merge call, because the damage happens AT the
    merge and is not undone by one: reopening the issue is a hand step the operator only takes
    once they notice. The session that hits this is usually not the session that wrote the
    body — the fan-out agent opens the PR on daniel-server and daniel-box lands it — so the
    message says what to rewrite rather than assuming the reader chose the wording.
    """
    stray = stray_closing_refs(body)
    if not stray:
        return
    lines = "\n  ".join(stray)
    ln.die(
        f"PR #{ln.opts.pr}'s body carries a closing keyword that is not a `Closes #N` line, "
        f"so merging it closes an issue this PR may not have fixed (issue #2513):\n"
        f"  {lines}\n"
        "Rewrite the reference without a closing keyword in front of the number — "
        '"Filed for later: #N" — or move a deliberate close onto its own `Closes #N` line, '
        "then `gh pr edit` the body and re-run this.",
        1,
    )


def _leave_for_a_direct_merge(ln: Landing, subject: str) -> None:
    """Hand the PR to await_merge, which merges it directly once CI is green.

    Without --await-merge nothing in this run would merge it, so that dies instead.
    """
    pr = ln.opts.pr
    if not ln.opts.await_merge:
        ln.die(
            f"PR #{pr} is merged into {BRANCH} only by --await-merge — re-run with both "
            "--arm-merge --await-merge, which merges it directly once CI is green; "
            "--await-merge alone only polls",
            1,
        )
    ln.direct_merge_subject = subject
    say(
        f"PR #{pr}: not arming auto-merge — merging directly once CI is green, at the head "
        "CI checked"
    )


def squash_body(commits: list[CommitMessage]) -> str:
    """The squash body that carries the PR's `Removes-Object:` lines, or '' when it has none.

    `promote.py decide` refuses a promotion that loses an object no such line declares, so a
    line that misses master stalls every later promotion. Without a body of ours, GitHub writes
    the repo's squash default, which a settings change can turn into the PR description or
    nothing. The body is therefore every commit's message, in GitHub's `* <headline>` form,
    then each line again in a final paragraph, where git's trailer parser also finds it. With
    no line, '' leaves GitHub's default alone.
    """
    messages = [
        f"* {c.get('messageHeadline', '')}\n\n{c.get('messageBody', '')}".rstrip()
        for c in commits
    ]
    lines = removes_object.declared("\n\n".join(messages))
    if not lines:
        return ""
    trailers = "\n".join(f"{removes_object.KEY}: {line}" for line in sorted(lines))
    return "\n\n".join([*messages, trailers]) + "\n"


def _merge_directly(ln: Landing, head: str) -> str:
    """Squash-merge the PR at `head` through the REST endpoint; '' on success, else why not.

    `sha` makes GitHub refuse if the head moved since await_ci read it green.
    `{owner}/{repo}` resolves from the checkout, as every `gh pr view` here does.
    A PR whose commits carry a `Removes-Object:` line also sends the body `squash_body` writes.
    """
    fields = [
        "merge_method=squash",
        f"sha={head}",
        f"commit_title={ln.direct_merge_subject}",
    ]
    body = squash_body(ln.view("commits").get("commits", []))
    if body:
        fields.append(f"commit_message={body}")
    try:
        ln.tools.gh(
            "api",
            "-X",
            "PUT",
            f"repos/{{owner}}/{{repo}}/pulls/{ln.opts.pr}/merge",
            *(arg for field in fields for arg in ("-f", field)),
        )
    except subprocess.CalledProcessError as exc:
        return (exc.stderr or "").strip() or f"gh exited {exc.returncode}"
    except subprocess.TimeoutExpired:
        return "gh timed out"
    say(f"merged directly at the checked head: {ln.direct_merge_subject}")
    return ""


def _master_paths_since(ln: Landing, head: str) -> list[str]:
    """The paths master changed since the base `head` was last tested against; [] when none did.

    That base is the master commit the head's green `ci.yml` PR run checked out
    (`tools.tested_base`), and the merge base of `head` and master when it cannot be read.

    GitHub's compare endpoint lists at most 300 files. A capped list is still non-empty, so a
    capped answer still reads as stale.

    Dies when the read fails: an unknown base is an unknown test result, and the cost is one
    landing to re-run.
    """
    # DECIDED: every path master moved counts as one the PR's tests read. The `pytest` job runs
    # unscoped over the whole tree, and both stale-base reds the issue cites came from a path
    # sharing no directory with the PR: #2922 (cc2a71fd8) added a size test while master grew the
    # claude-otel role's CLAUDE.md past it, and #2266 (b86d87d60) changed initial_setup
    # while master changed docs/facts.lock and ansible/roles/setup/k3s/CLAUDE.md. A directory
    # or overlap filter passes both. Only the base moves: a PR pushed after master moved was
    # tested against that newer master, so the merge base alone would update a branch CI had
    # already tested in full (149 of 235 landings in 2026-10-15..17, 642 CI minutes, #5487).
    # The tested base is the master commit in the checkout log's `Merge <head> into <base>`;
    # when it cannot be read the merge base stands, which costs one CI run at worst.
    base = ln.tools.tested_base(head) or head
    url = f"repos/{{owner}}/{{repo}}/compare/{base}...{BRANCH}"

    def call() -> list:
        got = ln.tools.gh_json("api", url, "--jq", "[.files[]?.filename]")
        return as_list(got, "gh api compare")

    try:
        paths = gh_read.read(call, ln.tools.sleep, "gh api compare")
    except gh_read.GhReadFailed as exc:
        ln.die(f"could not compare {head[:8]} with {BRANCH}: {exc}", 1)
    except ValueError as exc:
        ln.die(
            f"could not compare {head[:8]} with {BRANCH}: unparseable gh output: {exc}",
            1,
        )
    return [p for p in paths if isinstance(p, str)]


def _stale_paths(ln: Landing, head: str) -> list[str]:
    """The paths that make `head` stale enough to update; [] to merge it as it stands.

    Dies instead when `head` is one the operator approved and master moved past its base.
    """
    moved = _master_paths_since(ln, head)
    if moved and head == ln.approved_head:
        # DECIDED: a stale approved head is refused, not updated and not merged. The update
        # would make a head the operator's approval does not name, so the policy would refuse
        # it anyway, and merging it untested is what this check exists to stop. The rebase
        # moves the merge base to master, so the re-approved head goes stale only if master
        # moves again between the approval and the landing.
        ln.die(
            f"PR #{ln.opts.pr}'s base is behind {BRANCH} by {len(moved)} changed path(s), "
            "and its approved head cannot be updated without voiding the approval — rebase "
            f"it onto origin/{BRANCH}, push, get the new head approved, and re-run this",
            1,
        )
    return moved


def _update_branch(ln: Landing, head: str, moved: list[str]) -> None:
    """Merge master into the PR branch at `head`, so its CI runs against today's master.

    `expected_head_sha` makes GitHub refuse if the branch moved since `head` was read. A
    refusal dies: the PR cannot be re-tested from here, and merging it untested is what this
    check exists to stop.
    """
    shown = ", ".join(moved[:3]) + (
        f" and {len(moved) - 3} more" if len(moved) > 3 else ""
    )
    say(
        f"{BRANCH} changed {len(moved)} path(s) since PR #{ln.opts.pr}'s base ({shown}); "
        f"updating its branch so CI re-tests it against {BRANCH} before the merge"
    )
    try:
        ln.tools.gh(
            "api",
            "-X",
            "PUT",
            f"repos/{{owner}}/{{repo}}/pulls/{ln.opts.pr}/update-branch",
            "-f",
            f"expected_head_sha={head}",
        )
    except subprocess.CalledProcessError as exc:
        why = (exc.stderr or "").strip() or f"gh exited {exc.returncode}"
        ln.die(
            f"PR #{ln.opts.pr}'s base is behind {BRANCH} and GitHub refused to update its "
            f"branch ({why}) — rebase it onto origin/{BRANCH}, push, and re-run this",
            1,
        )
    except subprocess.TimeoutExpired:
        ln.die(
            f"PR #{ln.opts.pr}'s base is behind {BRANCH} and the branch update timed out — "
            f"rebase it onto origin/{BRANCH}, push, and re-run this",
            1,
        )


def _refuse_handback_while_flux_owns(ln: Landing, head: str) -> None:
    """Die before the merge while Flux still owns a tag this PR makes Ansible-owned.

    A handback is two PRs (docs/flux-deploy-plane.md, *Handing a service back*): the first
    sets `owner: released`, the root prunes the tag's Kustomization, and only then does the
    second remove `owner`. Merged while the Kustomization exists, the second hands the tag to
    Ansible while Flux still reconciles it, so two writers apply it. This compares `tag_owners`
    at the PR's merge base and at `head`, and for each tag that loses `owner`, reads the
    Kustomization with the read-only kubectl. Only `NotFound` lets the merge proceed. A read
    that fails refuses too, since it cannot rule the second writer out.

    The guard covers only a merge `land.sh` makes. A merge made elsewhere bypasses it, and the
    PR body's quoted `NotFound` is then the only guard (d2-first-leaf.md, *Rollback*).
    """
    pr = ln.opts.pr
    ln.git(
        "fetch", "-q", "--no-write-fetch-head", "origin", f"refs/pull/{pr}/head", BRANCH
    )
    base = ln.git("merge-base", f"origin/{BRANCH}", head)
    if base.returncode != 0 or not base.stdout.strip():
        ln.die(
            f"could not find PR #{pr}'s merge base with {BRANCH}, so whether it hands a "
            "tag back from Flux is unknown — not merging",
            1,
        )
    try:
        before = ln.tools.flux.owners_at(ln.opts.primary, base.stdout.strip())
        after = ln.tools.flux.owners_at(ln.opts.primary, head)
    except (ValueError, subprocess.SubprocessError, OSError) as exc:
        ln.die(
            f"could not read which tags Flux applies at PR #{pr}'s base or head, so "
            f"whether it hands a tag back is unknown — not merging: {exc}",
            1,
        )
    for tag in sorted(before.keys() - after.keys()):
        try:
            present = ln.tools.flux.kustomization_exists(tag)
        except Exception as exc:
            ln.die(
                f"PR #{pr} hands `{tag}` back to Ansible, and the Kustomization `{tag}` "
                f"could not be read — not merging: {exc}",
                1,
            )
        if present:
            ln.die(
                f"PR #{pr} hands `{tag}` back to Ansible while the Kustomization `{tag}` "
                "still exists in flux-system, so Flux and Ansible would both apply it. "
                "Merge the PR that sets `owner: released` first, wait until `kubectl get "
                f"kustomizations.kustomize.toolkit.fluxcd.io -n flux-system {tag}` returns "
                "NotFound, then re-run this",
                1,
            )
        say(f"`{tag}` is handed back to Ansible; its Kustomization is gone (NotFound)")


def _require_author(ln: Landing) -> None:
    """Die unless the PR's author is `opts.require_author`; a no-op when it is unset.

    Its own `gh pr view --json author` rather than a field on the state read, so a session
    with no author requirement makes no extra call. `login` is what
    `gh pr list --author` matches on too (`app/renovate` for the bot), so the unit's value
    and the wrapper's census name the author the same way.
    """
    want = ln.opts.require_author
    if not want:
        return
    have = (ln.view("author").get("author") or {}).get("login", "")
    if have != want:
        ln.die(
            f"authored by {have or '<unknown>'}, not {want} — this session may only arm "
            f"{want}'s PRs (LAND_REQUIRE_AUTHOR). Leave it open and file a hand-off finding "
            "carrying its land.sh command, so an interactive session lands it (#2746); do "
            "not pass --any-author from the session this variable is set in",
            1,
        )


def _refuse_private_repo(ln: Landing) -> None:
    """Die unless `repos/{owner}/{repo}` reads `visibility: public`.

    A failed or unparseable read dies too: an unknown visibility is an unknown merge gate,
    and the cost is one landing to re-run.
    """

    def call() -> JsonObject:
        got = ln.tools.gh_json("api", "repos/{owner}/{repo}")
        return as_object(got or {}, "gh api repos")

    try:
        repo = gh_read.read(call, ln.tools.sleep, "gh api repos")
    except gh_read.GhReadFailed as exc:
        ln.die(f"could not read the repo's visibility: {exc}", 1)
    except ValueError as exc:
        ln.die(f"could not read the repo's visibility: unparseable gh output: {exc}", 1)
    visibility = repo.get("visibility") or "<unknown>"
    if visibility != "public":
        ln.die(
            f"repo is {visibility}: rulesets are not enforced on the free plan, so the CI "
            f"merge gate is open — not merging PR #{ln.opts.pr} until the repo is public "
            "again",
            1,
        )


def arm_merge(ln: Landing) -> None:
    """Check this PR and leave it for await_merge to merge, unless it is already merged."""
    pr = ln.opts.pr
    view = ln.view("state,title,body,reviewDecision")
    if view.get("state") == "MERGED":
        say("already merged; --arm-merge is a no-op")
        return
    if view.get("state") == "CLOSED":
        ln.die(f"PR #{pr} was closed without merging — nothing to arm", 1)
    _require_author(ln)
    _refuse_stray_closing_refs(ln, view.get("body") or "")
    _refuse_private_repo(ln)
    if ln.opts.policy_active:
        ln.pinned_head = policy.check(ln)
    subject = ln.opts.subject or view.get("title", "")
    review = view.get("reviewDecision")
    if review == "CHANGES_REQUESTED":
        ln.die(
            f"PR #{pr} has changes requested — address them and get a new review, then "
            "re-run this",
            1,
        )
    _leave_for_a_direct_merge(ln, subject)


_RETRIGGER_DONE = (
    "The landing already converted it to draft and back once for this head. "
)


def _draft_skipped_dead_end(ln: Landing, head: str, retriggered: bool = False) -> str:
    """Why a ready PR whose head's CI skipped as a draft cannot merge; "" while it is a draft.

    PR #6069 was marked ready one second after its push, and GitHub created only the push's
    draft-era run (#6077). No run followed the ready flip, so nothing would ever supply a
    verdict. A PR still a draft gets its run once someone marks it ready, so the wait goes on.
    """
    if ln.view("isDraft").get("isDraft") is not False:
        return ""
    pr = ln.opts.pr
    return (
        f"PR #{pr} is ready, but {head[:8]}'s only CI ran while it was a draft and skipped "
        "every job, and GitHub started no run on the ready flip; a re-run replays the draft "
        f"event and skips again. {_RETRIGGER_DONE if retriggered else ''}Convert it to draft and back "
        f"(`gh pr ready --undo {pr}`, then `gh pr ready {pr}`) or push, then re-run this"
    )


def _ready_run_on(ln: Landing, head: str) -> str:
    """Why the PR's `ci.yml` runs on `head` forbid the draft toggle; "" when every one skipped.

    A run that is not completed, or that completed with any conclusion but `skipped`, ran
    while the PR was ready. Its `prek` gate `needs` every other job, so the gate gets no check
    run until they finish, and for those minutes `await_ci` reads only the draft run's skipped
    gate. Converting to draft then cancels the ready run (#6566). A failed read also forbids
    it, because the toggle is the one action here that can cancel a run.
    """
    url = f"repos/{{owner}}/{{repo}}/actions/workflows/ci.yml/runs?head_sha={head}&event=pull_request"

    def call() -> list:
        got = ln.tools.gh_json(
            "api", url, "--jq", "[.workflow_runs[] | {id, status, conclusion}]"
        )
        return as_list(got, "gh api ci.yml runs")

    try:
        runs = gh_read.read(call, ln.tools.sleep, "gh api ci.yml runs")
    except (gh_read.GhReadFailed, ValueError) as exc:
        return f"its CI runs could not be read ({exc})"
    for run in runs:
        if not isinstance(run, dict):
            return f"its CI runs read as {runs!r}"
        if run.get("status") != "completed" or run.get("conclusion") != "skipped":
            return (
                f"run {run.get('id')} is {run.get('conclusion') or run.get('status')}"
            )
    return ""


def _retrigger_ready_run(ln: Landing, head: str) -> str:
    """Convert the PR to draft and back so GitHub starts a ready run; "" on success.

    GitHub starts no `ci.yml` run for the ready flip of a PR whose push run skipped as a
    draft (#6546), and a re-run replays the draft event. The conversion is the only thing
    that makes one, so the landing does it once per head instead of ending `merge-timeout`
    for a person to do the same two commands. A failure is returned as the reason, and names
    the PR's state when the first command had already run, because a draft PR never merges.
    """
    pr = str(ln.opts.pr)
    say(
        f"PR #{pr} is ready but {head[:8]}'s only CI skipped as a draft; converting it to draft and back"
    )
    for step, args in (
        ("to draft", ("pr", "ready", "--undo", pr)),
        ("back to ready", ("pr", "ready", pr)),
    ):
        try:
            ln.tools.gh(*args)
        except subprocess.CalledProcessError as exc:
            why = (exc.stderr or "").strip() or f"gh exited {exc.returncode}"
            left = (
                ""
                if step == "to draft"
                else f"; PR #{pr} is left as a draft, run `gh pr ready {pr}`"
            )
            return f"converting it {step} failed: {why}{left}"
        except subprocess.TimeoutExpired:
            return f"converting it {step} timed out"
    return ""


def await_merge(ln: Landing) -> None:
    """Poll until merged, merging directly once CI is green. Bail early on two dead ends.

    Only CONFLICTING may bail, and only on two consecutive polls: GitHub computes
    mergeability asynchronously and serves UNKNOWN until it settles, and master moving under
    the PR flips the field for one poll. A red PR CI is the other dead end; GitHub says only
    `BLOCKED`, the same word it uses while checks run, so await_ci owns that verdict,
    one-shot. Only its exit 1 bails: `pending` IS the grace period, derived rather than
    guessed.

    A PR arm_merge left for a direct merge is merged here, on the first poll where await_ci
    reads its head green, unless master moved past the PR's base: then the branch is updated
    once and the merge waits for the updated head to go green. A green head also waits while
    master CI has a backlog (`pace.hold`). A held poll never ends the wait at `merge_timeout`:
    once held and unheld time reach it, the merge goes ahead without pacing.
    Without --arm-merge this only polls, for a PR merged some other way.

    A head whose every CI run skipped as a draft is named on the poll that first reads it. Once
    it has stayed so for `DRAFT_SKIPPED_GRACE_S`, a PR that is no longer a draft is converted
    to draft and back once for that head (`_retrigger_ready_run`) and the grace starts again.
    If the head is still skipped after it, the wait ends (`_draft_skipped_dead_end`). Neither
    happens while a ready `ci.yml` run exists on the head (`_ready_run_on`); the grace restarts.
    """
    o, t = ln.opts, ln.tools
    waited = 0
    conflicting = 0
    last_refusal = ""
    # await_ci's last answer when it was `pending`, for the merge-timeout message; else empty.
    ci_pending = ""
    # The pacing hold the last green poll printed (`pace.py`), for the merge-timeout message.
    paced = ""
    # Seconds of polls `pace.hold` held, kept out of `waited` because they raise the pacing cap.
    held = 0
    # The head this landing updated the branch from; empty until it does.
    updated_from = ""
    # The head the handback refusal and the pin gate last passed, so a merge GitHub refuses
    # and the wait retries reads kubectl, runs the gates and posts the status again only for a
    # new head.
    handback_checked = ""
    # The forward-only pins that head bumps, whose land rule is read again before each retry.
    gated_pins: list[str] = []
    # The head await_ci last read as skipped in a draft, and the `waited` it was first read at.
    draft_head = ""
    draft_since = 0
    # The head this landing converted to draft and back, once (#6546); a second draft-skip on
    # it ends the wait, because another conversion would loop.
    retriggered_head = ""
    while True:
        # This poll's pacing hold; a held poll never ends the wait at merge-timeout.
        hold = ""
        view = ln.view("state,mergeable,headRefOid")
        state = view.get("state", "")
        if state == "MERGED":
            break
        if state == "CLOSED":
            ln.die(f"PR #{o.pr} was closed without merging — nothing to land", 1)
        conflicting = conflicting + 1 if view.get("mergeable") == "CONFLICTING" else 0
        if conflicting >= 2:
            ln.die(
                f"PR #{o.pr} conflicts with {BRANCH} — rebase it, push, then re-run this",
                1,
                Verdict.MERGE_CONFLICT,
            )
        head = view.get("headRefOid") or ""
        # GitHub applies a branch update asynchronously. Until the head moves, the head on
        # the PR is the stale one, and its green CI is what the update exists to discard.
        if updated_from and head == updated_from:
            head = ""
        if ln.pinned_head and head and head != ln.pinned_head:
            if updated_from != ln.pinned_head:
                ln.die(
                    f"PR #{o.pr}'s head moved from {ln.pinned_head[:8]} to {head[:8]} after "
                    "the landing policy checked it; re-run so the new head is checked",
                    1,
                )
            # The move is this landing's own update, so the policy checks the new head and
            # the merge is pinned to it.
            say(f"checking the landing policy again on the updated head {head[:8]}")
            ln.pinned_head = policy.check(ln)
            continue
        if head:
            rc, line = t.await_ci(head, 0)
            ci_pending = line if rc == CI_PENDING else ""
            if await_ci.DRAFT_SKIPPED not in ci_pending:
                draft_head = ""
            elif head != draft_head:
                draft_head, draft_since = head, waited
                say(f"{line}; waiting up to {DRAFT_SKIPPED_GRACE_S}s for a ready run")
            elif waited - draft_since >= DRAFT_SKIPPED_GRACE_S:
                dead_end = _draft_skipped_dead_end(ln, head, head == retriggered_head)
                ready_run = _ready_run_on(ln, head) if dead_end else ""
                if ready_run:
                    say(
                        f"{head[:8]} reads as skipped as a draft, but {ready_run}; "
                        "not converting it to draft, which would cancel that run"
                    )
                    draft_since = waited
                elif dead_end and head != retriggered_head:
                    retriggered_head = head
                    failure = _retrigger_ready_run(ln, head)
                    if failure:
                        ln.die(f"{dead_end}; {failure}", 75, Verdict.MERGE_TIMEOUT)
                    # The new run takes a moment to register, so the grace starts again.
                    draft_since = waited
                elif dead_end:
                    ln.die(dead_end, 75, Verdict.MERGE_TIMEOUT)
            if rc == CI_RED:
                ln.die(
                    f"PR #{o.pr} cannot merge — its own CI is red ({line}); fix it, push, "
                    "and re-run this",
                    1,
                    Verdict.PR_CI_RED,
                )
            # Only this landing's direct merge waits on await_ci's green, so only it can
            # never fire on a disarmed gate; a merge made some other way keeps the wait.
            if rc == CI_DISARMED and ln.direct_merge_subject:
                ln.die(disarmed(f" at {head[:8]}", f"PR #{o.pr} not merged"), 1)
            hold = (
                pace.hold(ln, held, waited)
                if rc == CI_GREEN and ln.direct_merge_subject
                else ""
            )
            if hold and hold != paced:
                say(hold)
            paced = hold
            if rc == CI_GREEN and ln.direct_merge_subject and not hold:
                moved = [] if updated_from else _stale_paths(ln, head)
                if moved:
                    _update_branch(ln, head, moved)
                    updated_from = head
                else:
                    if head != handback_checked:
                        _refuse_handback_while_flux_owns(ln, head)
                        gated_pins = pin_gate.gate_before_merge(ln, head)
                        handback_checked = head
                    else:
                        pin_gate.recheck_before_merge(ln, gated_pins)
                    refusal = _merge_directly(ln, head)
                    if not refusal:
                        continue
                    if refusal != last_refusal:
                        say(
                            f"GitHub refused the direct merge at {head[:8]}; waiting: "
                            f"{refusal}"
                        )
                        last_refusal = refusal
        if not hold and waited + held >= o.merge_timeout:
            # A run cancelled on the head never supplies a verdict on its own (#5931). A run
            # skipped as a draft names its own remedy in await_ci's line, because a re-run of
            # it skips again (#6077).
            if draft_head:
                why = f"its head has no CI verdict ({ci_pending})"
            elif ci_pending:
                why = (
                    f"its head has no CI verdict ({ci_pending}); if its run was cancelled, "
                    "none arrives on its own: re-run it with `gh run rerun <run-id>`, or push"
                )
            else:
                why = paced or "look at its checks or the queue"
            ln.die(
                f"PR #{o.pr} still {state} after {o.merge_timeout}s — not being merged; {why}",
                75,
                Verdict.MERGE_TIMEOUT,
            )
        t.sleep(o.merge_poll)
        if hold:
            held += o.merge_poll
        else:
            waited += o.merge_poll
    paced_note = f", {held}s of it held by pacing" if held else ""
    say(f"merged after {waited + held}s{paced_note}")
