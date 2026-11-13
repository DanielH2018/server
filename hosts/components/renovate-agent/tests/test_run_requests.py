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
import run_requests

REPO = "o/r"
CENSUS = {71, 80}


class _Gh:
    def __init__(self, rc=0):
        self.calls, self.rc = [], rc

    def __call__(self, argv, cwd=None, timeout=120, stdin_text=None):
        self.calls.append(argv)
        if argv[:3] == ["gh", "pr", "view"]:
            return 0, json.dumps({"headRefOid": "c" * 40})
        if argv[:3] == ["gh", "run", "list"]:
            return 0, json.dumps([{"databaseId": 9}])
        return self.rc, ""


def _run(tmp_path, gh=None):
    gh = gh or _Gh()
    requests = run_requests.fresh_dir(str(tmp_path))
    return requests, gh


def _carry(tmp_path, requests, gh):
    tools = agent_toolbox.AgentTools(run=gh)
    return run_requests.carry_out(requests, CENSUS, REPO, tools, str(tmp_path))


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
