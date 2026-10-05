"""The recorder publish_pr's tests drive `publish` and `unlanded` with, and its answers."""

import subprocess

import publish_pr


def cp(rc: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)


# `gh pr create` prints the PR's URL, and the landing needs the number in it.
DEFAULT_ANSWERS = {"gh pr create": cp(out="https://github.com/o/r/pull/42\n")}


class Recorder:
    """A Tools pair that answers from a per-command table and records every call in order.

    An answer may be an exception instance instead of a completed process, which the runner
    raises. That is the only way to reach the timeout path: ``lib.gh.gh`` bounds every call at
    60s, and a stub that actually sleeps past it is not a test anyone can run.
    """

    def __init__(
        self,
        answers: dict[str, subprocess.CompletedProcess[str] | Exception] | None = None,
    ):
        self.answers = answers or {}
        self.calls: list[tuple[str, ...]] = []

    def _run(self, tool: str, *args: str) -> subprocess.CompletedProcess[str]:
        self.calls.append((tool, *args))
        # Keyed on the verb: "git push", or "gh pr create" since every gh call starts with "pr".
        key = " ".join((tool, *args[: 2 if tool == "gh" else 1]))
        answer = self.answers.get(key, DEFAULT_ANSWERS.get(key, cp()))
        if isinstance(answer, Exception):
            raise answer
        return answer

    def tools(self) -> publish_pr.PublishTools:
        # **kw absorbs the `timeout=` the ls-remote pre-flight passes; the stub never blocks,
        # so the value is nothing to record.
        return publish_pr.PublishTools(
            git=lambda *a, **kw: self._run("git", *a),
            gh=lambda *a, **kw: self._run("gh", *a),
            land=lambda *a, **kw: self._run("land", *a),
        )


TIMEOUT = subprocess.TimeoutExpired(cmd=["gh"], timeout=60.0)
