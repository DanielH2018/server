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
    """Answers run(host, command) from a table, records every call."""

    answers: dict[str, subprocess.CompletedProcess] = field(default_factory=dict)
    calls: list[tuple[str, str, str | None]] = field(default_factory=list)

    def __call__(self, host, command, timeout, stdin=None):
        self.calls.append((host, command, stdin))
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
