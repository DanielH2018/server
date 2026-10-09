"""The fake runner and pipeline builder the review pipeline's tests share."""

import json
import subprocess

from fanout_lib.brief import Issue, render_brief
from fanout_lib.red_gate import Gates
from fanout_lib.review import Pipeline
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
    }


class FakeRunner:
    def __init__(self, worktree, reports, heads=("aaa", "bbb")):
        self.worktree = worktree
        self.reports = list(reports)
        self.heads = list(heads)
        self.claude = []  # (argv, stdin, phase file at call time)
        self.comments = []
        self.git = []

    def __call__(self, argv, stdin):
        if argv[0] == "git":
            self.git.append(argv[3:])
            if "merge-base" in argv:
                out = "base0"
            elif "rev-parse" in argv:
                out = self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]
            else:
                out = ""
            return subprocess.CompletedProcess(argv, 0, out + "\n", "")
        if argv[0] == "gh":
            self.comments.append(stdin)
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "uv":
            self.derived = argv
            return subprocess.CompletedProcess(argv, 0, SECRET_LISTING, "")
        phase = (self.worktree / ".fanout" / "phase").read_text().strip()
        self.claude.append((argv, stdin, phase))
        return subprocess.CompletedProcess(argv, 0, json.dumps(self.reports.pop(0)), "")


def _pipeline(
    tmp_path,
    reports,
    host="daniel-box",
    heads=("aaa", "bbb"),
    clock=None,
    gates=None,
    target=SERVER_TARGET,
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
    )
    return pipeline, run
