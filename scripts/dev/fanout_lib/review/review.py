"""The review pipeline a `launch --review` batch runs in place of a single `claude -p`.

WHY. Without this module a fan-out batch implements, tests, opens its PR and lands it in one
context, and no other context reads the change (`docs/failure-classes.md` class 1, "Findings get an
adversarial pass; fixes get none"). This module puts a separate review between the PR and the
landing. The lint and test checks that finish in seconds stay with the implementer; only the
slow model review moves here.

THE PHASES, each one `claude -p` in the batch's worktree:

1. implement — the brief on stdin. A `--review` brief stops the agent at its PR, a draft here.
2. review — a fresh session with the issue text and the diff only, never the implementer's
   transcript. Edit and Write are denied to it, and it returns findings as structured output.
   Before it, this repo's batch runs `base_check`, and the reviewer is told which of the PR's
   new tests pass with its code changes taken out.
3. fix — only when a finding passes `actionable`; `fix_round` runs it. It resumes the
   implementer session.
4. delta review — a fresh reviewer reads the fix's commits inside the whole change. A finding
   it reports is left only if it quotes a line the fix head's file has (`delta_confirm`);
   the rest are carried unverified and hold nothing. A finding of medium severity or
   worse left by it earns one more fix and delta review, and no more.
4b. ready — on a host that does not land, `review_ci.ready_gate` marks a draft PR ready and
   waits for its CI run.
5. land — on the deploy host, the pipeline lands the PR as the draft it is
   (`review_ci.land_draft`, which runs `land.sh` through `review_land`). The landing marks it
   ready or tests it in a landing batch, so its first CI run is the one it merges on. A landing
   that ends `pr-ci-red` resumes the implementer once, as the `ci-fix` phase, and the PR is
   landed again. The implementer is resumed as the `apply` phase only for a verdict that
   needs a decision. Before the landing, and on every other host instead of it, a session
   resumes to file what is left.
5b. CI wait — on a host that does not land, for this repo's batch only, `review_ci`
   waits for the PR's CI with `cc-wait gh-ci --pr` before the comment and before the filing
   session. A red verdict resumes the implementer once, as the `ci-fix` phase, with each failed
   check's name and log tail, and the wait runs again on the new head. A resume that pushed
   nothing re-runs the failed jobs once before that wait. A batch still red, or given no
   verdict within `review_ci.CI_WAIT_S` (`CI_WAIT_MAX_S` while its checks are still running),
   ends `needs input:` and nothing lands it. A
   `cancelled` run is no verdict and is waited on again. The result goes in the record as
   `ci_gate` and on the PR comment.

DECIDED: the fix round resumes the implementer session rather than starting a fresh one. The
implementer already holds the change's context, and the delta review is the independent check
on the fix. A fresh fixer would re-read the whole change for no extra separation.

THE STOP HOOK. `.claude/hooks/fanout-stop.py` fires in every session under the worktree. The
pipeline writes the running phase to `.fanout/phase` and resets the hook's block counter before
each call. The hook never blocks a `review` phase, whose final message is JSON. Every other
phase owes a PR URL; the landing's `VERDICT:` line is in its log, which the pipeline wrote.

WHAT THE IMPLEMENTER CAN WRITE. The worktree, and for another repo's batch the `.fanout/server`
snapshot this module runs from. The pipeline therefore reads the review prompt, the headless
system prompt, this repo's `.claude/settings.json` and every hook once, at start.
Each phase gets the prompts as text and runs the Stop hook from a copy outside
the worktree that is rewritten before every call. In this repo's batch, every phase after the
implementer also loads no settings file from the worktree, and the reviewer gets the start-time
`CLAUDE.md` as text; `held_hooks` says why. The pipeline kills whatever a test run it
starts leaves running (`processes.reaping`).

DISCLOSURE. The repo is public. A finding in category `security` reaches the PR comment as a
count only, is never filed with `findings.py open`, and is kept in full only in the local
record under `STATE_DIR`.

The local record also carries each phase's cost and the finding counts, and it outlives the
worktree that `clean` removes. `scripts/dev/fanout.py stats` sums the records.
"""

import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fanout_lib.branch_pr import OwnPr, needs_input_report, worktree_own_pr
from fanout_lib.brief import lands
from fanout_lib.review.held_hooks import (
    labelled,
    pointed_settings,
    read_hooks,
    read_rules,
    rules_text,
    with_batch_env,
    write_hooks,
)
from fanout_lib.review.review_prompts import (
    FINDINGS_SCHEMA,
    anti_patterns,
    file_prompt,
    is_held,
    apply_prompt,
    review_prompt,
)
from fanout_lib.review.fix_round import fix_rounds
from fanout_lib.review.base_check import BaseCheck, unproven_tests
from fanout_lib.review.processes import otel_attributes, reaping, run_process
from fanout_lib.review.review_ci import (
    awaits_ci,
    ci_gate,
    land_draft,
    ready_gate,
    server_lands,
)
from fanout_lib.review.review_land import (
    LAND_MARGIN_S,
    RESUME_VERDICTS,
    out_of_time,
    release,
)
from fanout_lib.review.review_land import report as landing_report
from fanout_lib.review.review_record import (
    Phase,
    Record,
    actionable,
    comment_body,
    findings_of,
    held_for_findings,
    issues_section,
)
from fanout_lib.launch import (
    REVIEW_RUNTIME_MAX_S,
    SYSTEM_PROMPT_FILE,
    stop_hook_settings,
)
from fanout_lib.prompt_tool import prompt_tool_args
from fanout_lib.sizing import REVIEWER
from fanout_lib.target import Target

PROMPT_FILE = Path(__file__).resolve().parent / "review_system_prompt.md"
# The checkout this module was loaded from: the batch worktree for this repo, the batch's
# `.fanout/server` snapshot for another repo. Both are trees the implementer can write.
SOURCE_ROOT = Path(__file__).resolve().parents[4]
HEADLESS_PROMPT_FILE = SOURCE_ROOT / SYSTEM_PROMPT_FILE
# Names the pipeline's own copy of `fanout-stop.py`. The worktree's copy, which this repo's
# `.claude/settings.json` registers too, stands down while the file exists, so one Stop spends
# the block cap once. `.claude/hooks/fanout-stop.py` mirrors the path.
OWN_COPY = Path(".fanout") / "stop-hook"
STATE_DIR = Path.home() / ".local" / "state" / "fanout-review"

# One process boundary for claude, git and gh: argv and stdin in, the finished process out.
Runner = Callable[[list[str], str | None], subprocess.CompletedProcess]


class Pipeline:
    """One batch's phases, run in order against one worktree.

    Args:
        worktree: the batch's worktree, the cwd of every call.
        batch: the batch id.
        host: the host this runs on, which decides whether the batch lands.
        target: the repo the batch works.
        brief: the brief text the unit fed on stdin.
        run: the process boundary; tests pass a fake.
        clock: seconds since some fixed point; tests pass a fake.
        state_dir: where the local record goes.
        base_check: which new tests pass without the PR's code changes; tests script it.
        sleep: the pause between CI waits that ended without a verdict; tests pass a fake.
    """

    def __init__(
        self,
        worktree: Path,
        batch: str,
        host: str,
        target: Target,
        brief: str,
        run: Runner = run_process,
        clock: Callable[[], float] = time.monotonic,
        state_dir: Path = STATE_DIR,
        base_check: Callable[..., BaseCheck] = unproven_tests,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.worktree = worktree
        self.batch = batch
        self.host = host
        self.target = target
        self.brief = brief
        self.run = run
        self.clock = clock
        self.deadline = clock() + REVIEW_RUNTIME_MAX_S
        self.state_dir = state_dir
        self.base_check = base_check
        self.sleep = sleep
        # Read at start for every reviewer: the skill lives outside the worktree, but one
        # read keeps every phase on the same text.
        self.anti_patterns = anti_patterns()
        self.record = Record(batch)
        self.session = ""
        # The implementer session's `total_cost_usd` so far: a resumed session reports its
        # whole-session total, so each resumed phase records only the increase.
        self.session_cost = 0.0
        # Read now, before the implementer runs. A server unit imports this module from the
        # batch worktree, which that agent can write, so a file read at review time would
        # take whatever the implementer left there. The headless prompt and the Stop hook
        # are held the same way for every phase the implementer's session resumes into: each
        # phase gets the prompt as text and runs the hook from `hook_root`,
        # which `_snapshot_hook` rewrites from these bytes before every call.
        self.review_prompt = PROMPT_FILE.read_text()
        self.headless_prompt = HEADLESS_PROMPT_FILE.read_text()
        self.hooks = read_hooks(SOURCE_ROOT)
        # Another repo's snapshot has no `.claude/settings.json`; its worktree's are its own.
        self.project_settings = (
            (SOURCE_ROOT / ".claude" / "settings.json").read_text()
            if target.is_server
            else ""
        )
        # The reviewer loads no project source, so no `CLAUDE.md`: it gets this copy as text.
        self.project_claude_md = (
            (SOURCE_ROOT / "CLAUDE.md").read_text() if target.is_server else ""
        )
        # A held phase loads no `.claude/rules/`, so it gets their text too (G5 PR 0).
        self.rules = read_rules(SOURCE_ROOT) if target.is_server else {}
        # The merge base the review starts from; it fixes the range that selects the rules.
        self.range_base = ""
        self.hook_root = state_dir / f"{batch}-stop-hook"

    def _claude(self, name: str, argv: list[str], stdin: str) -> Phase:
        fanout = self.worktree / ".fanout"
        fanout.mkdir(exist_ok=True)
        (self.worktree / OWN_COPY).write_text(
            f"{write_hooks(self.hooks, self.hook_root)}\n"
        )
        (fanout / "phase").write_text(
            f"{'review' if name.startswith('review') else name}\n"
        )
        (fanout / "stop-blocks").write_text("0\n")
        started = self.clock()
        proc = self.run(labelled(argv, otel_attributes(self.batch, name)), stdin)
        try:
            report = json.loads(proc.stdout)
        except json.JSONDecodeError:
            report = {}
        if not isinstance(report, dict) or not report:
            report = {
                "type": "result",
                "is_error": True,
                "subtype": f"no report (exit {proc.returncode})",
                "result": proc.stderr.strip()[-2000:],
            }
        (fanout / f"{name}.json").write_text(json.dumps(report))
        phase = Phase(name, report)
        cost = phase.cost
        if name == "implement" or "--resume" in argv:
            # A failed resume can report no total at all, which is no reason to forget the
            # session's spend so far.
            cost = max(phase.cost - self.session_cost, 0.0)
            self.session_cost = max(self.session_cost, phase.cost)
        record = self.record
        record.costs[name] = record.costs.get(name, 0) + cost
        record.durations[name] = round(
            record.durations.get(name, 0) + self.clock() - started, 1
        )
        denials = report.get("permission_denials")
        record.permission_denials[name] = record.permission_denials.get(name, 0) + (
            len(denials) if isinstance(denials, list) else 0
        )
        return phase

    def _read_at(self, rev: str, path: str) -> str | None:
        """`path` as commit `rev` holds it, or None when it has no such file."""
        shown = self.run(
            ["git", "-C", str(self.worktree), "show", f"{rev}:{path}"], None
        )
        return shown.stdout if shown.returncode == 0 else None

    def _git(self, *args: str) -> str:
        return self.run(["git", "-C", str(self.worktree), *args], None).stdout.strip()

    def _stop_hook(self) -> list[str]:
        """The prefix that runs `claude` under the held Stop hook and this host's pytest width.

        `launch.claude_args` names the hook inside a tree the agent can write, so a later phase
        would run whatever the agent left there. It runs on the placing host, which knows the
        wrong core count.
        """
        settings = json.dumps(with_batch_env(stop_hook_settings(str(self.hook_root))))
        return ["claude", "--settings", settings]

    def _held_settings(self) -> list[str]:
        """The prefix a phase after the implementer runs `claude` with.

        For this repo's batch it loads no project or local settings file, and passes the
        project settings read at start, each hook command pointed at the copy in `hook_root`.
        That set already registers `fanout-stop`, so the Stop hook is not added a second time.
        Another repo's batch keeps `_stop_hook`'s prefix and its own project settings: the
        pipeline holds no copy of that repo's hooks, so dropping the source would drop them.
        """
        if not self.project_settings:
            return self._stop_hook()
        settings = json.dumps(pointed_settings(self.project_settings, self.hook_root))
        return ["claude", "--setting-sources", "user", "--settings", settings]

    def _implementer(
        self, prefix: list[str] | None = None, prompt: str = ""
    ) -> list[str]:
        return [
            *(prefix or self._stop_hook()),
            "-p", "--model", self.record.implementer.model, "--permission-mode", "auto",
            *prompt_tool_args(), "--output-format", "json", "--max-budget-usd",
            str(self.record.implementer.budget_usd), "--append-system-prompt",
            self.headless_prompt + prompt, "--effort", self.record.implementer.effort,
        ]  # fmt: skip

    def _resume(self) -> list[str]:
        held = self._implementer(self._held_settings(), self._rules())
        return [*held, "--resume", self.session]

    def _reviewer(self) -> list[str]:
        prompt = self.review_prompt
        if self.anti_patterns:
            prompt += "\n\n# What a vacuous test looks like\n\n" + self.anti_patterns
        if self.project_claude_md:
            prompt += (
                "\n\n# The repo's CLAUDE.md, as it stood before the implementer ran\n\n"
                + self.project_claude_md
            )
        prompt += self._rules()
        return [
            *self._held_settings(),
            "-p", "--model", REVIEWER.model, "--permission-mode", "auto", *prompt_tool_args(),
            "--output-format", "json", "--max-budget-usd", str(REVIEWER.budget_usd),
            "--append-system-prompt", prompt,
            "--disallowedTools", "Edit,Write,NotebookEdit", "--effort", self.record.review_effort,
            "--json-schema", json.dumps(FINDINGS_SCHEMA),
        ]  # fmt: skip

    def _rules(self) -> str:
        """The `.claude/rules/` text a held phase gets, matched to the batch's range.

        The range is `range_base` to `HEAD`. It holds the implementer's edits, so it selects
        the rules for what a phase reviews or fixes. A resumed phase passes the same text
        again, because `--resume` keeps no appended system prompt.
        """
        if not self.range_base or not self.rules:
            return rules_text(self.rules, None)
        diff = self._git("diff", "--name-only", "-z", self.range_base, "HEAD")
        return rules_text(self.rules, diff.split("\0"))

    def _unproven(self, base: str, head: str) -> list[str]:
        """Record which of the PR's new tests pass without its code changes, and name them.

        Every batch of this repo runs it. Another repo's batch runs none, because the check
        runs this repo's pytest configuration. It only records, so any exception it raises
        lands in `base_error` and the batch goes on to its review.
        """
        if not self.target.is_server:
            return []
        try:
            # It runs whole every test file that holds a new test, which the implementer wrote.
            with reaping():
                check = self.base_check(self.run, self.worktree, base, head)
        # A measure never stops the batch, so this catches what the check did not expect.
        except Exception as exc:
            check = BaseCheck(error=f"{type(exc).__name__}: {exc}")
        self.record.base_tests = check.new
        self.record.base_passing = check.passing
        self.record.base_error = check.error
        return check.passing

    def _save(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = self.state_dir / f"{self.batch}-{stamp}.json"
        path.write_text(json.dumps(asdict(self.record), indent=2))

    def run_all(self) -> dict:
        """Run every phase and return the report the unit writes to `report.json`.

        That report is the last phase's, so `status` reads it exactly as it reads a
        single-session batch: a PR URL, a `VERDICT:` line, or a blocker line.
        """
        issues = issues_section(self.brief)
        impl = self._claude("implement", self._implementer(), self.brief)
        found = OwnPr()
        if not impl.failed:
            found = worktree_own_pr(
                impl.text, self.worktree, self.target.repo, self.run
            )
        # A text naming only another PR is a defect: end here naming both.
        report = needs_input_report(found.problem) if found.problem else impl.report
        if impl.failed or not found.url:
            # Saved all the same: a batch that never reached a PR is the failure rate the
            # records exist to measure.
            self.record.settle(report, has_pr=False)
            self._save()
            return report
        self.record.pr = found.url
        self.session = str(impl.report.get("session_id") or "")
        base = self.range_base = self._git("merge-base", "HEAD", self.target.base)
        head = self.record.head = self._git("rev-parse", "HEAD")

        passing = self._unproven(base, head)
        review = self._claude(
            "review", self._reviewer(), review_prompt(issues, base, head, passing)
        )
        found, error = findings_of(review)
        self.record.review_error = error
        self.record.findings = found or []
        self.record.actionable = actionable(self.record.findings)
        last = impl
        if self.record.actionable and self.session:
            last = fix_rounds(self, issues, base, head, last)

        held = held_for_findings(self.record, self.state_dir)
        # A PR this pipeline lands stays a draft for `land_draft`. Any other is marked ready,
        # a held one included, since a PR left a draft gets no CI.
        lands_it = held is None and (
            lands(self.host, self.target.repo) or server_lands(self, self.record.head)
        )
        ready = None if lands_it else ready_gate(self)
        final = held or ready
        if final is None and not lands_it and awaits_ci(self.host, self.target):
            # Before the comment, so the comment carries the CI gate beside the others.
            last, final = ci_gate(self, last)
        comment = ["gh", "pr", "comment", self.record.pr, "--body-file", "-"]
        self.run(comment, comment_body(self.record))
        final = final or self._finish(last, lands_it)
        self.record.settle(final, has_pr=True)
        self._save()
        held = sum(1 for f in self.record.remaining if is_held(f))
        if held:
            final = dict(final)
            final["result"] = (
                f"{held} unresolved security findings are held off the public tracker; "
                f"read them in {self.state_dir}.\n{final.get('result') or ''}"
            )
        return final

    def time_left(self) -> float:
        return self.deadline - self.clock()

    def _finish(self, last: Phase, lands_it: bool) -> dict:
        if lands_it:
            if self.time_left() < LAND_MARGIN_S:
                return out_of_time(self.record.pr)
            # Filed before the merge, so the PR body names each leftover's issue.
            self._file_leftovers()
            landing, still_red = land_draft(self)
            self.record.verdict = landing.line or f"none (exit {landing.rc})"
            try:
                if still_red:
                    return still_red
                if landing.verdict in RESUME_VERDICTS and self.session:
                    log_dir = str(self.worktree / ".fanout")
                    prompt = apply_prompt(self.record.pr, landing, log_dir)
                    return self._claude("apply", self._resume(), prompt).report
                return landing_report(self.record.pr, landing)
            finally:
                release(self.run, self.worktree, landing)
        return self._file_leftovers() or last.report

    def _file_leftovers(self) -> dict | None:
        """Resume the implementer to file the public leftovers; its report, or None if none."""
        if any(not is_held(f) for f in self.record.remaining) and self.session:
            return self._claude("file", self._resume(), file_prompt(self.record)).report
        return None
