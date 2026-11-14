"""The runner carries out a session's request files, and refuses the ones it must not read.

#967's I3: session output is untrusted data. These plant each shape the plan names (a symlink,
a FIFO, a file over the cap, a PR outside the census) in a real directory, and read what the
runner would hand `gh` from `AgentTools.run`.

Run: uv run pytest hosts/components/renovate-agent/tests/test_run_requests.py
"""

import json
import os

import agent_toolbox
import pytest
import renovate_agent
import renovate_session
import run_requests

REPO = "o/r"
CENSUS = {71, 80, 81, 82}
EPOCH = 1_800_000_000.0
SETTLED = "VERDICT: settled (PR #71, abc, tags: , hosts: daniel-box)"


class _Gh:
    """Answers gh and systemctl; a lander start writes `verdicts[pr]` and ends with `results[pr]`."""

    def __init__(self, rc=0, verdicts=None, results=None):
        self.calls, self.rc = [], rc
        self.verdicts, self.results = verdicts or {}, results or {}
        self.written: dict[str, str] = {}

    def read(self, path):
        return self.written.get(os.path.basename(path), "")

    def landed(self):
        return [
            int(a[2].split("@")[1].split(".")[0])
            for a in self.calls
            if a[:2] == ["systemctl", "start"]
        ]

    def __call__(self, argv, cwd=None, timeout=120, stdin_text=None):
        self.calls.append(argv)
        if argv[:2] == ["systemctl", "start"]:
            pr = int(argv[2].split("@")[1].split(".")[0])
            self.written[f"{pr}.verdict"] = self.verdicts.get(pr, SETTLED) + "\n"
            return 0, ""
        if argv[:2] == ["systemctl", "show"]:
            pr = int(argv[-1].split("@")[1].split(".")[0])
            return 0, f"Result={self.results.get(pr, 'success')}\nExecMainStatus=0\n"
        if argv[:3] == ["gh", "pr", "view"]:
            return 0, json.dumps({"headRefOid": "c" * 40})
        if argv[:3] == ["gh", "run", "list"]:
            return 0, json.dumps([{"databaseId": 9}])
        return self.rc, ""


def _run(tmp_path, gh=None):
    gh = gh or _Gh()
    requests = run_requests.fresh_dir(str(tmp_path))
    return requests, gh


def _carry(tmp_path, requests, gh, max_lands=3, deadline=None):
    tools = agent_toolbox.AgentTools(run=gh, read_file=gh.read)
    return run_requests.carry_out(
        requests,
        CENSUS,
        REPO,
        tools,
        str(tmp_path),
        max_lands=max_lands,
        deadline=EPOCH + 3600 if deadline is None else deadline,
        clock=lambda: EPOCH,
    )


def _nominate(requests, *prs):
    for place, pr in enumerate(prs, 1):
        _write(requests, f"{place}-{pr}-land.json", {"action": "land", "pr": pr})


def _lander(tmp_path, **kwargs):
    return _Gh(**kwargs)


def _write(requests, name, request):
    with open(os.path.join(requests, name), "w") as f:
        json.dump(request, f)


def test_a_close_and_a_comment_are_carried_out_and_logged(tmp_path):
    requests, gh = _run(tmp_path)
    _write(
        requests,
        "71-close.json",
        {"action": "close", "pr": 71, "body": "Superseded by #80."},
    )
    _write(requests, "80-comment.json", {"action": "comment", "pr": 80, "body": "hi"})
    outcomes = _carry(tmp_path, requests, gh)
    assert [o.done for o in outcomes] == [True, True]
    assert [
        "gh",
        "pr",
        "close",
        "71",
        "--repo",
        REPO,
        "--comment",
        "Superseded by #80.",
    ] in gh.calls
    assert ["gh", "pr", "comment", "80", "--repo", REPO, "--body", "hi"] in gh.calls
    logged = [
        json.loads(line)
        for line in (tmp_path / "requests.jsonl").read_text().splitlines()
    ]
    assert [(r["action"], r["pr"], r["done"]) for r in logged] == [
        ("close", 71, True),
        ("comment", 80, True),
    ]


def test_a_rerun_re_runs_the_failed_runs_at_the_head(tmp_path):
    requests, gh = _run(tmp_path)
    _write(requests, "71-rerun.json", {"action": "rerun", "pr": 71})
    [outcome] = _carry(tmp_path, requests, gh)
    assert outcome.done
    assert ["gh", "run", "rerun", "9", "--failed", "--repo", REPO] in gh.calls


def _plant(requests, shape):
    path = os.path.join(requests, "71-close.json")
    if shape == "symlink":
        target = os.path.join(os.path.dirname(requests), "elsewhere.json")
        with open(target, "w") as f:
            json.dump({"action": "close", "pr": 71}, f)
        os.symlink(target, path)
    elif shape == "fifo":
        os.mkfifo(path)
    elif shape == "oversized":
        with open(path, "w") as f:
            json.dump(
                {"action": "comment", "pr": 71, "body": "x" * 3000, "pad": "y" * 20000},
                f,
            )
    elif shape == "outside the census":
        _write(requests, "71-close.json", {"action": "close", "pr": 99})
    elif shape == "unknown action":
        _write(requests, "71-close.json", {"action": "merge", "pr": 71})


@pytest.mark.parametrize(
    "shape, names",
    [
        ("symlink", "a symlink"),
        ("fifo", "not a regular file"),
        ("oversized", "over 16384 bytes"),
        ("outside the census", "not an open Renovate PR"),
        ("unknown action", "is not one of"),
    ],
)
def test_a_request_the_runner_must_not_trust_is_refused_without_a_gh_call(
    tmp_path, shape, names
):
    requests, gh = _run(tmp_path)
    _plant(requests, shape)
    [outcome] = _carry(tmp_path, requests, gh)
    assert not outcome.done and names in outcome.detail
    assert gh.calls == []


def test_a_leftover_requests_directory_is_set_aside_unread(tmp_path):
    requests, _ = _run(tmp_path)
    _write(requests, "71-close.json", {"action": "close", "pr": 71})
    again = run_requests.fresh_dir(str(tmp_path))
    assert again == requests and os.listdir(again) == []


@pytest.mark.parametrize(
    "verdict",
    [
        SETTLED,
        "VERDICT: nothing-to-deploy (PR #71 touched no service)",
        "REFUSED: PR #71 changes inventory/",
    ],
)
def test_the_next_nomination_lands_after_one_that_ends_well(tmp_path, verdict):
    requests, _ = _run(tmp_path)
    gh = _lander(tmp_path, verdicts={71: verdict})
    _nominate(requests, 71, 80)
    _carry(tmp_path, requests, gh)
    assert gh.landed() == [71, 80]


@pytest.mark.parametrize(
    "verdict, result",
    [
        ("VERDICT: failed (PR #71, health gate red)", "exit-code"),
        (
            "VERDICT: refused (PR #71 is merged, and this landing graded nothing)",
            "exit-code",
        ),
        ("PENDING", "exit-code"),
        (SETTLED, "timeout"),
    ],
)
def test_a_landing_that_does_not_end_well_stops_the_rest(tmp_path, verdict, result):
    requests, _ = _run(tmp_path)
    gh = _lander(tmp_path, verdicts={71: verdict}, results={71: result})
    _nominate(requests, 71, 80)
    outcomes = _carry(tmp_path, requests, gh)
    assert gh.landed() == [71]
    assert "#71 stopped the run" in outcomes[-1].detail


def test_nominations_past_the_cap_are_not_landed(tmp_path):
    requests, _ = _run(tmp_path)
    gh = _lander(tmp_path)
    _nominate(requests, 71, 80, 81)
    outcomes = _carry(tmp_path, requests, gh, max_lands=2)
    assert gh.landed() == [71, 80]
    assert "over the run's 2 landings" in outcomes[-1].detail


def test_a_nomination_for_a_pr_the_run_closed_is_not_landed(tmp_path):
    requests, _ = _run(tmp_path)
    gh = _lander(tmp_path)
    _write(
        requests, "71-close.json", {"action": "close", "pr": 71, "body": "Superseded."}
    )
    _nominate(requests, 71, 80)
    _carry(tmp_path, requests, gh)
    assert gh.landed() == [80]


def test_no_landing_starts_after_the_runs_deadline(tmp_path):
    requests, _ = _run(tmp_path)
    gh = _lander(tmp_path)
    _nominate(requests, 71)
    [outcome] = _carry(tmp_path, requests, gh, deadline=EPOCH - 1)
    assert gh.landed() == []
    assert "deadline passed" in outcome.detail


class _Tick:
    """Every process one tick reaches: #71 is open until its landing merges it."""

    def __init__(self, state, output):
        self.merged, self.posts = False, []
        self.state, self.output = state, output

    def read(self, path):
        if path.endswith("/71.verdict"):
            return SETTLED + "\n" if self.merged else ""
        return agent_toolbox.read_file(path)

    def run(self, argv, cwd=None, timeout=120, stdin_text=None):
        if argv[:3] == ["gh", "pr", "list"]:
            prs = (
                []
                if self.merged
                else [
                    {
                        "number": 71,
                        "title": "Update x",
                        "url": "u/71",
                        "author": {"login": "app/renovate"},
                    }
                ]
            )
            return 0, json.dumps(prs)
        if argv[:3] == ["gh", "pr", "view"]:
            return 0, json.dumps({"state": "MERGED"})
        if argv == ["systemctl", "start", renovate_agent.SESSION_UNIT]:
            (self.state / "requests" / "1-71-land.json").write_text(
                json.dumps({"action": "land", "pr": 71})
            )
            (self.state / renovate_session.OUTPUT_FILE).write_text(self.output)
            return 0, ""
        if argv[:2] == ["systemctl", "start"]:
            self.merged = True
            return 0, ""
        if argv[:2] == ["systemctl", "show"]:
            return 0, "Result=success\nExecMainStatus=0\n"
        if "list" in argv and "--porcelain" in argv:
            return (
                0,
                f"worktree {argv[2]}/.claude/worktrees/renovate-auto\nHEAD abc\n\n",
            )
        if "rev-list" in argv:
            return 0, "0\n"
        if "rev-parse" in argv:
            return 0, "0123abcd\n"
        return 0, ""

    def discord_post(self, webhook, text, ua, log=None, spool_dir=None):
        self.posts.append(text)
        return True


def test_a_pr_the_session_nominates_lands_and_reaches_the_digest(tmp_path, monkeypatch):
    """The after-census runs after the landings, so the headline counts what they merged."""
    monkeypatch.setenv("RENOVATE_SESSION_GH_TOKEN", "gh-read-sentinel")
    (tmp_path / ".claude" / "worktrees" / "renovate-auto").mkdir(parents=True)
    state = tmp_path / "state"
    result = json.dumps(
        {"type": "result", "subtype": "success", "result": "Nominated #71."}
    )
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("/renovate-prs\n")
    cfg = tmp_path / "config.env"
    cfg.write_text(
        f"REPO=o/r\nREPO_DIR={tmp_path}\nPROMPT_FILE={prompt}\n"
        f"STATE_DIR={state}\nCLAUDE_BIN={tmp_path}/claude\n"
    )
    tick = _Tick(state, result)
    tools = renovate_agent.AgentTools(
        run=tick.run,
        read_file=tick.read,
        discord_post=tick.discord_post,
        flush_discord_spool=lambda *a, **k: True,
    )
    assert renovate_agent.main(tools, str(cfg)) == 0
    [digest] = tick.posts
    assert digest.startswith("✅ renovate-agent resolved #71"), digest


def test_the_journal_line_for_a_request_carries_no_session_text(tmp_path):
    """A file name or a refused value is session text; only fixed words reach the log (#8676)."""
    requests, _ = _run(tmp_path)
    _write(requests, "x: status=down.json", {"action": "x: status=down", "pr": 71})
    _nominate(requests, 71)
    lines = [
        renovate_agent.request_line(o)
        for o in _carry(tmp_path, requests, _lander(tmp_path))
    ]
    assert lines == ["request refused #0 not done", "request land #71 done"]
