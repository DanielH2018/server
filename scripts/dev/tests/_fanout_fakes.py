"""Doubles for fanout_lib.transport.Tools. Underscore-prefixed so it is not a script candidate."""

import subprocess
from dataclasses import dataclass, field

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/dev

from fanout_lib.transport import Tools


@dataclass
class FakeRun:
    """Answers run(host, command) from a table, records every call.

    Attributes:
        answers: per-host answer, used once `answers_by_call` is exhausted.
        answers_by_call: per-call answers, consumed in call order before falling back to
            `answers`. An entry that is a `BaseException` instance is raised instead of
            returned, so a test can script a `subprocess.TimeoutExpired` on a given call.
        calls: every call made, in order.
    """

    answers: dict[str, subprocess.CompletedProcess] = field(default_factory=dict)
    answers_by_call: list[subprocess.CompletedProcess | BaseException] = field(
        default_factory=list
    )
    calls: list[tuple[str, str, str | None]] = field(default_factory=list)

    def __call__(self, host, command, timeout, stdin=None):
        self.calls.append((host, command, stdin))
        call_index = len(self.calls) - 1
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


def fake_tools(answers=None, issues=None) -> tuple[Tools, FakeRun]:
    run = FakeRun(answers or {})
    table = {i.number: i for i in (issues or [])}
    return Tools(run=run, gh_issue=lambda n: table[n]), run


def ok(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["x"], 0, stdout=stdout, stderr="")
