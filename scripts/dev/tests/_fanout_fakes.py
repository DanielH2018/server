"""Doubles for fanout_lib.transport.Tools. Underscore-prefixed so it is not a script candidate."""

import subprocess
from dataclasses import dataclass, field

from fanout_lib.target import SERVER_CHECKOUT
from fanout_lib.transport import Tools

OPERATOR_CHECKOUT = "/home/ubuntu/server"


def as_operator(text: str) -> str:
    """`text` with this run's server checkout written as the operator's `/home/ubuntu/server`.

    A command the fan-out builds names `SERVER_CHECKOUT`, which is the checkout the suite runs
    in (a CI runner's, for one). The tests spell their expectations against the operator's path,
    so each normalises the command here instead of repeating the derivation in every assertion.
    """
    return text.replace(SERVER_CHECKOUT, OPERATOR_CHECKOUT)


# A syntactically real ed25519 public key line (32 zero bytes), standing in for a host's
# signing key, plus the registered set it is a member of. Both are test data: the gate
# compares what a host prints against what GitHub answers, never against a constant.
HOST_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    " ubuntu@fake-host"
)
REGISTERED_KEYS = frozenset({" ".join(HOST_KEY.split()[:2])})


@dataclass
class FakeRun:
    """Answers run(host, command) from a table, records every call.

    Attributes:
        answers: per-host answer, used once `answers_by_call` is exhausted.
        answers_by_call: per-call answers, consumed in host-call order before falling back to
            `answers`. A `findings` call is not a host call and takes no entry. An entry that is a `BaseException` instance is raised instead of
            returned, so a test can script a `subprocess.TimeoutExpired` on a given call.
        calls: every call made, in order.
        issue_fetches: every `(number, repo)` issue fetch `fake_tools` answered.
    """

    answers: dict[str, subprocess.CompletedProcess] = field(default_factory=dict)
    answers_by_call: list[subprocess.CompletedProcess | BaseException] = field(
        default_factory=list
    )
    calls: list[tuple[str, str, str | None]] = field(default_factory=list)
    issue_fetches: list[tuple[int, str]] = field(default_factory=list)

    @property
    def host_calls(self) -> list[tuple[str, str, str | None]]:
        """`calls` without the `findings` ones, for a test that counts ssh and local calls."""
        return [call for call in self.calls if call[0] != "findings"]

    def __call__(self, host, command, timeout, stdin=None):
        self.calls.append((host, command, stdin))
        call_index = len(self.host_calls) - 1
        if call_index < len(self.answers_by_call):
            answer = self.answers_by_call[call_index]
            if isinstance(answer, BaseException):
                raise answer
            return answer
        if host in self.answers:
            return self.answers[host]
        return subprocess.CompletedProcess(
            [host], 255, stdout="", stderr="ssh: connect: refused"
        )


def fake_tools(
    answers=None,
    issues=None,
    issue_errors=None,
    signing_keys=None,
    signing_error=None,
    merged_prs=None,
    findings_exit=0,
    refused_claims=(),
    head="worktree-orch",
) -> tuple[Tools, FakeRun]:
    """Build a Tools whose boundaries answer from tables, plus the FakeRun behind it.

    Args:
        answers: per-host answer for `run`, as `FakeRun.answers`.
        issues: the issues `gh_issue` returns, looked up by number.
        issue_errors: exceptions `gh_issue` raises instead, keyed by issue number — a fetch
            that fails part-way through a run is what the hoisted fetch has to survive.
        signing_keys: the normalized signing keys GitHub verifies for the account; defaults
            to the one HOST_KEY is, so a test that says nothing about signing passes the gate.
        signing_error: an exception `signing_keys` raises instead, for the refusal path.
        merged_prs: branch -> merged PR url, the forge answer `merged_pr` gives. A branch
            absent from it reads as "no merged PR", which is also what a failed `gh` call
            reads as in the real thing.
        findings_exit: the exit status every `findings.py` call returns. Each call is recorded
            in the FakeRun's `calls` under the pseudo-host `findings`.
        refused_claims: issue numbers whose `claim` exits 3 instead, refused.
        head: what `head_branch` answers, or an exception it raises instead.

    Returns:
        The Tools and the FakeRun it holds, so a test can script and read the calls.
    """
    run = FakeRun(answers or {})
    table = {i.number: i for i in (issues or [])}
    errors = issue_errors or {}

    def gh_issue(number: int, repo: str = "DanielH2018/server"):
        run.issue_fetches.append((number, repo))
        if number in errors:
            raise errors[number]
        return table[number]

    def keys():
        if signing_error is not None:
            raise signing_error
        return frozenset(REGISTERED_KEYS if signing_keys is None else signing_keys)

    prs = merged_prs or {}

    def merged_pr(branch: str, repo: str = "DanielH2018/server") -> str:
        return prs.get(branch, "")

    def findings(argv: list[str]) -> subprocess.CompletedProcess:
        # Recorded among the host calls, under the pseudo-host `findings`, so a test reads
        # where the claim fell relative to the tree and the agent from one list.
        run.calls.append(("findings", " ".join(argv), None))
        if argv[0] == "claim" and any(str(n) in argv[1:] for n in refused_claims):
            return subprocess.CompletedProcess(
                argv, 3, stdout=f"#{argv[1]} refused: held by `other`\n", stderr=""
            )
        return subprocess.CompletedProcess(argv, findings_exit, stdout="", stderr="")

    def head_branch() -> str:
        if isinstance(head, BaseException):
            raise head
        return head

    def default_ref(checkout: str) -> str:
        return "origin/main"

    return Tools(
        run=run,
        gh_issue=gh_issue,
        signing_keys=keys,
        merged_pr=merged_pr,
        findings=findings,
        default_ref=default_ref,
        head_branch=head_branch,
    ), run


def ok(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["x"], 0, stdout=stdout, stderr="")
