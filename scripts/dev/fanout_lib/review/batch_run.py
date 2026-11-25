"""One review batch's worktree, record and deadline, and the calls each phase makes there.

`review.Pipeline` runs the phases in order over a `BatchRun`. The phase helpers it calls
(`fix_round`, `review_ci`, `conflict_resume`) take the same object and reach only what this
module defines, so none of them imports the pipeline that calls it.
"""

import json
import time
from collections.abc import Callable
from pathlib import Path

from dev.fanout_lib.launch import (
    REVIEW_RUNTIME_MAX_S,
    SYSTEM_PROMPT_FILE,
    stop_hook_settings,
)
from dev.fanout_lib.prompt_tool import prompt_tool_args
from dev.fanout_lib.review.base_check import BaseCheck, unproven_tests
from dev.fanout_lib.review.held_hooks import (
    labelled,
    pointed_settings,
    read_hooks,
    read_rules,
    rules_text,
    with_batch_env,
    write_hooks,
)
from dev.fanout_lib.review.processes import Runner, run_process
from dev.fanout_lib.telemetry import otel_attributes
from dev.fanout_lib.review.review_prompts import (
    FINDINGS_SCHEMA,
    anti_patterns,
    system_prompt,
)
from dev.fanout_lib.review.review_record import Phase, Record
from dev.fanout_lib.sizing import REVIEWER
from dev.fanout_lib.target import Target

# The checkout this module was loaded from; both the worktree and the snapshot are trees the
# implementer can write.
SOURCE_ROOT = Path(__file__).resolve().parents[4]
HEADLESS_PROMPT_FILE = SOURCE_ROOT / SYSTEM_PROMPT_FILE
# The worktree's own copy of `fanout-stop.py` stands down while this file exists, so one Stop spends
# the block cap once. `.claude/hooks/fanout-stop.py` mirrors the path.
OWN_COPY = Path(".fanout") / "stop-hook"
STATE_DIR = Path.home() / ".local" / "state" / "fanout-review"

# One process boundary for claude, git and gh: argv and stdin in, the finished process out.


class BatchRun:
    """One batch's worktree and record, and the `claude` and `git` calls its phases make.

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
        self.anti_patterns = anti_patterns()
        self.record = Record(batch)
        self.session = ""
        # A resumed session reports its whole-session total, so each resumed phase records only the
        # increase.
        self.session_cost = 0.0
        # Read before the implementer runs: a server unit imports this module from the batch
        # worktree, which that agent can write. The headless prompt and Stop hook are held the same
        # way; each phase runs the hook from `hook_root`, rewritten from these bytes before every
        # call.
        self.review_prompt = system_prompt()
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
        # A held phase loads no `.claude/rules/`, so it gets their text.
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

    def time_left(self) -> float:
        return self.deadline - self.clock()
