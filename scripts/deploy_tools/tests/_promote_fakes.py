"""A fake forge and a real throwaway master for `promote.py`'s tests.

`FakeTransport` stands in for ghcr.io and api.github.com behind the real `forge.Registry` and
`forge.GitHub`, so the tests exercise forge's status-to-outcome mapping as well as `decide`.
It records every request. `FakeRun` answers the `gh variable get` and `cosign verify`
subprocesses. `World` builds a linear master in a temporary git repository, with the
contexts file `promote.py` reads, and runs `promote.py decide` against all three.
"""

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import forge
import promote

CONTEXT = "prek (lint + validate + tests + secrets)"
CONTEXTS_YAML = f"gitops_deploy_ci_contexts:\n  - {CONTEXT}\n"


def digest_of(name: str) -> str:
    return "sha256:" + hashlib.sha256(name.encode()).hexdigest()


def green(name: str = CONTEXT) -> list[dict]:
    return [{"name": name, "status": "completed", "conclusion": "success"}]


@dataclass
class FakeTransport:
    """ghcr.io and api.github.com in one callable.

    Attributes:
        tags: each published tag and the digest it names.
        manifests: the manifest body a GET of each tag returns.
        check_runs: each commit's check-runs.
        forced: a status to answer for a manifest reference, overriding the tables.
        requests: every `(method, url)` sent, in order.
    """

    tags: dict[str, str] = field(default_factory=dict)
    manifests: dict[str, dict] = field(default_factory=dict)
    check_runs: dict[str, list[dict]] = field(default_factory=dict)
    forced: dict[str, int] = field(default_factory=dict)
    requests: list[tuple[str, str]] = field(default_factory=list)

    def __call__(self, method, url, headers):
        self.requests.append((method, url))
        parts = urlsplit(url)
        if parts.netloc == forge.REGISTRY:
            return self._registry(method, parts.path)
        match = re.fullmatch(r"/repos/[^/]+/[^/]+/commits/(\w+)/check-runs", parts.path)
        if parts.netloc == "api.github.com" and match:
            runs = self.check_runs.get(match.group(1), [])
            return forge.Response(200, body=json.dumps({"check_runs": runs}).encode())
        return forge.Response(404)

    def _registry(self, method, path):
        if path == "/token":
            return forge.Response(200, body=b'{"token": "registry-token"}')
        if path.endswith("/tags/list"):
            return forge.Response(
                200, body=json.dumps({"tags": sorted(self.tags)}).encode()
            )
        reference = path.rsplit("/manifests/", 1)[1]
        if reference in self.forced:
            return forge.Response(self.forced[reference])
        if reference not in self.tags:
            unknown = {"errors": [{"code": "MANIFEST_UNKNOWN"}]}
            return forge.Response(404, body=json.dumps(unknown).encode())
        headers = {"docker-content-digest": self.tags[reference]}
        if method == "HEAD":
            return forge.Response(200, headers)
        body = json.dumps(self.manifests.get(reference, {})).encode()
        return forge.Response(200, headers, body)

    def registry_requests(self) -> list[tuple[str, str]]:
        """The requests sent to ghcr.io, as `(method, path)`."""
        return [
            (method, urlsplit(url).path)
            for method, url in self.requests
            if urlsplit(url).netloc == forge.REGISTRY
        ]


@dataclass
class FakeRun:
    """`subprocess.run` for `gh variable get` and `cosign verify`.

    Attributes:
        hold: DEPLOY_HOLD's value, or None for a missing variable.
        hold_fails: make `gh` fail with an error other than "not found".
        cosign_fails: make every `cosign verify` exit 1.
        verified: each digest cosign was asked to verify.
    """

    hold: str | None = "none"
    hold_fails: bool = False
    cosign_fails: bool = False
    verified: list[str] = field(default_factory=list)

    def __call__(self, argv, **_kwargs):
        if argv[:3] == ["gh", "variable", "get"]:
            if self.hold_fails:
                return subprocess.CompletedProcess(
                    argv, 1, "", "HTTP 401: Bad credentials"
                )
            if self.hold is None:
                return subprocess.CompletedProcess(
                    argv, 1, "", f"variable {argv[3]} was not found\n"
                )
            return subprocess.CompletedProcess(argv, 0, self.hold + "\n", "")
        if argv[:2] == ["cosign", "verify"]:
            self.verified.append(argv[-1].rsplit("@", 1)[1])
            if self.cosign_fails:
                return subprocess.CompletedProcess(argv, 1, "", "no signatures found")
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected subprocess {argv}")


def _git(repo: Path, *args: str) -> str:
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "HOME": str(repo),
        "PATH": "/usr/bin:/bin",
    }
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


@dataclass
class Result:
    """One `promote.py decide` run: its exit code, its stdout line, stderr and step outputs."""

    exit_code: int
    line: str
    stderr: str
    outputs: dict[str, str]


class World:
    """A master branch of `size` commits, oldest first in `commits`, and a forge to match."""

    def __init__(
        self, tmp_path: Path, size: int = 4, contexts_yaml: str = CONTEXTS_YAML
    ):
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        self.tmp_path = tmp_path
        _git(self.repo, "init", "-q", "-b", "master")
        contexts = self.repo / promote.CI_CONTEXTS_SOURCE
        contexts.parent.mkdir(parents=True)
        contexts.write_text(contexts_yaml)
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-q", "-m", "c0")
        for n in range(1, size):
            _git(self.repo, "commit", "-q", "--allow-empty", "-m", f"c{n}")
        self.commits = _git(self.repo, "rev-list", "--reverse", "master").split()
        self.transport = FakeTransport()
        self.run = FakeRun()

    def branch_commit(self) -> str:
        """A commit off master's root, on no path master's history takes."""
        _git(self.repo, "checkout", "-q", "-b", "side", self.commits[0])
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "side")
        sha = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "-q", "master")
        return sha

    def publish(self, sha: str, check_runs: list[dict] | None = None) -> None:
        """Push and sign `sha-<sha>`, with green CI unless `check_runs` says otherwise."""
        self.transport.tags[f"sha-{sha}"] = digest_of(sha)
        self.transport.check_runs[sha] = green() if check_runs is None else check_runs

    def set_prod(self, sha: str, revision: str | None = None) -> None:
        """Point `prod` at `sha`, annotated with `revision` when given."""
        self.transport.tags["prod"] = digest_of(sha)
        value = f"master@sha1:{sha}" if revision is None else revision
        annotations = {promote.REVISION_ANNOTATION: value} if value else {}
        self.transport.manifests["prod"] = {"annotations": annotations}

    def clients(self, registry: forge.Registry | None = None) -> forge.Forge:
        return forge.Forge(
            registry=registry or forge.Registry(self.transport, "job-token"),
            github=forge.GitHub(self.transport, "job-token", run=self.run),
            cosign=forge.Cosign(run=self.run),
        )

    def decide(self, monkeypatch, capsys, trigger: str = "", registry=None) -> Result:
        output = self.tmp_path / "github_output"
        output.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
        argv = [
            "decide",
            "--trigger",
            trigger,
            "--repo",
            str(self.repo),
            "--master-ref",
            "master",
        ]
        exit_code = promote.main(argv, f=self.clients(registry))
        captured = capsys.readouterr()
        stdout = captured.out.splitlines()
        assert len(stdout) == 1, stdout
        outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
        return Result(exit_code, stdout[0], captured.err, outputs)
