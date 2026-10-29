"""The fake runner and pipeline builder the review pipeline's tests share."""

import json
import subprocess

from fanout_lib.review.base_check import BaseCheck
from fanout_lib.brief import Issue, render_brief
from fanout_lib.review.hunk_check import HunkCheck
from fanout_lib.review.red_gate import Gates
from fanout_lib.review.review import Pipeline
from fanout_lib.target import SERVER_TARGET

PR = "https://github.com/DanielH2018/server/pull/4000"
# What `secret_bearing_host_paths.py` prints: `dest<TAB>name,name` per line.
SECRET_LISTING = "/usr/local/bin/a.sh\tone_token,two_token\n"
ISSUES = [Issue(1345, "Traefik startupProbe has no red-proof", "body one")]


def _report(result="", session="sid-1", structured=None, is_error=False, cost=1.0):
    out = {
        "type": "result",
        "result": result,
        "session_id": session,
        "is_error": is_error,
        "total_cost_usd": cost,
    }
    if structured is not None:
        out["structured_output"] = structured
    return out


def _finding(title, severity="high", confidence=0.9, category="correctness"):
    return {
        "title": title,
        "file": "scripts/x.py",
        "line": 3,
        "severity": severity,
        "confidence": confidence,
        "category": category,
        "detail": "fails on an empty list",
        # The line the delta review quotes; `FakeRunner.tree` holds it at the fix head.
        "evidence": "boom = 1",
    }


def unprefixed(argv):
    """`argv` from its command, past a leading `env K=V ...`.

    `hardened_runs.hardened` puts one before `git`, and `Review._claude` one before `claude`
    carrying the OTEL attributes.
    """
    if argv[0] != "env":
        return argv
    rest = argv[1:]
    while rest and "=" in rest[0]:
        rest = rest[1:]
    return rest


class FakeRunner:
    def __init__(self, worktree, reports, heads=("aaa", "bbb")):
        self.worktree = worktree
        self.reports = list(reports)
        self.heads = list(heads)
        self.claude = []  # (argv, stdin, phase file at call time)
        self.comments = []
        self.git = []
        # What `git show <rev>:<path>` answers: the file at the fix head, by path. A path
        # absent from it has no such file there.
        self.tree = {"scripts/x.py": "a\nb\nboom = 1\nd\n"}
        # The landing the pipeline runs: each `bash -l` command, the VERDICT line land.sh's
        # log ends on ("" for none), land.sh's exit code, and cc-wait's exit codes in order.
        self.lands = []
        # What `gh issue view --json labels` answers for every issue of the batch.
        self.labels = {"labels": []}
        self.verdict = "VERDICT: settled (deployed)"
        self.land_rc = 0
        self.waits = [0]
        # The CI wait a batch that does not land runs: each `cc-wait gh-ci` command, the
        # (exit code, output) it answers with in order (the last repeats), the PR's
        # `statusCheckRollup`, and each failed job's log by job id.
        self.ci_calls = []
        self.ci = [(0, "WAIT: passed abcd1234: 20 check runs passed\n")]
        self.rollup = []
        self.job_logs = {}
        # The draft step: whether `gh pr view` reports a draft, each `gh pr ready` call, and
        # the `ci.yml` run ids each `gh api .../runs` read answers with in order (the last
        # repeats).
        self.draft = False
        self.readied = []
        self.ci_runs = [[1]]
        # Every `gh pr ready`, run-id read and `cc-wait gh-ci` call, in the order they ran.
        self.log = []

    def __call__(self, argv, stdin):
        argv = unprefixed(argv)
        if argv[0] == "git":
            self.git.append(argv[3:])
            if argv[3] == "show":
                path = argv[4].partition(":")[2]
                if path not in self.tree:
                    return subprocess.CompletedProcess(
                        argv, 128, "", "fatal: no such path"
                    )
                return subprocess.CompletedProcess(argv, 0, self.tree[path], "")
            if "merge-base" in argv:
                out = "base0"
            elif argv[-2:] == ["rev-parse", "HEAD"]:
                out = self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]
            else:
                out = ""
            return subprocess.CompletedProcess(argv, 0, out + "\n", "")
        if argv[:3] == ["gh", "issue", "view"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.labels), "")
        if argv[:3] == ["gh", "pr", "view"]:
            out = json.dumps(
                {
                    "statusCheckRollup": self.rollup,
                    "isDraft": self.draft,
                    "headRefOid": "bbb",
                }
            )
            return subprocess.CompletedProcess(argv, 0, out, "")
        if argv[:3] == ["gh", "pr", "ready"]:
            self.readied.append(argv)
            self.log.append(("undo" if "--undo" in argv else "ready",))
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:2] == ["gh", "api"] and "/actions/workflows/ci.yml/runs" in argv[2]:
            ids = self.ci_runs.pop(0) if len(self.ci_runs) > 1 else self.ci_runs[0]
            self.log.append(("runs", ids))
            return subprocess.CompletedProcess(argv, 0, json.dumps(ids), "")
        if argv[:3] == ["gh", "run", "view"]:
            log = self.job_logs.get(argv[4], "")
            return subprocess.CompletedProcess(argv, 0, log, "")
        if argv[0] == "gh":
            self.comments.append(stdin)
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "bash":
            return self._landing(argv)
        if argv[0] == "uv":
            self.derived = argv
            return subprocess.CompletedProcess(argv, 0, SECRET_LISTING, "")
        phase = (self.worktree / ".fanout" / "phase").read_text().strip()
        self.claude.append((argv, stdin, phase))
        return subprocess.CompletedProcess(argv, 0, json.dumps(self.reports.pop(0)), "")

    def _landing(self, argv):
        command = argv[argv.index("land") + 1 :]
        if "gh-ci" in command:
            self.ci_calls.append(command)
            self.log.append(("gh-ci",))
            rc, out = self.ci.pop(0) if len(self.ci) > 1 else self.ci[0]
            return subprocess.CompletedProcess(argv, rc, out, "")
        self.lands.append(command)
        if command[0].endswith("land.sh"):
            if self.land_rc == 0:
                log = self.worktree / ".fanout" / "land4000-20261010T000000Z.log"
                log.write_text(f"  waiting for CI\n{self.verdict}\n")
            return subprocess.CompletedProcess(
                argv, self.land_rc, "", "refused: bad body"
            )
        rc = self.waits.pop(0) if len(self.waits) > 1 else self.waits[0]
        return subprocess.CompletedProcess(argv, rc, "", "")


def _pipeline(
    tmp_path,
    reports,
    host="daniel-box",
    heads=("aaa", "bbb"),
    clock=None,
    gates=None,
    target=SERVER_TARGET,
    base_check=None,
    hunk_check=None,
    sleep=None,
):
    (tmp_path / ".fanout").mkdir()
    brief = render_brief(ISSUES, host, "1345", "worktree-orch", [], review=True)
    run = FakeRunner(tmp_path, reports, heads)
    pipeline = Pipeline(
        tmp_path,
        "1345",
        host,
        target,
        brief,
        run=run,
        clock=clock or (lambda: 0.0),
        state_dir=tmp_path / "state",
        red_green=gates is not None,
        gates=gates or Gates(),
        base_check=base_check or (lambda *_: BaseCheck()),
        hunk_check=hunk_check or (lambda *_: HunkCheck()),
        sleep=sleep or (lambda _: None),
    )
    return pipeline, run
