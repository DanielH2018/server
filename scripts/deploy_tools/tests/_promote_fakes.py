"""A fake forge and a real throwaway master for `promote.py`'s tests.

`FakeTransport` stands in for ghcr.io and api.github.com behind the real `forge.Registry` and
`forge.GitHub`, so the tests exercise forge's status-to-outcome mapping as well as `decide`
and `prune`. It records every request. `FakeRun` answers the `gh variable get` and `cosign verify`
subprocesses. `World` builds a linear master in a temporary git repository, with the
contexts file `promote.py` reads, and runs `promote.py decide` against all three.
"""

import hashlib
import io
import json
import re
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import forge
import promote

CONTEXT = "prek (lint + validate + tests + secrets)"
CONTEXTS_YAML = f"gitops_deploy_ci_contexts:\n  - {CONTEXT}\n"


def digest_of(name: str) -> str:
    return "sha256:" + hashlib.sha256(name.encode()).hexdigest()


# The bundle a commit with no published tag renders: D1's empty root and the renderer's
# digests.json for no tag, as promote.yml writes them.
EMPTY_ROOT = (
    b"apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources: []\n"
)
EMPTY_DIGESTS = {"_unclassified": {"footprint": "e3b0", "own": "e3b0", "paths": []}}


def bundle_files(digests: dict | None = None, **flux: str) -> dict[str, bytes]:
    """A bundle: `digests.json` (omitted when None), the kustomize root, and `flux/<name>.yaml`."""
    files = {"flux/kustomization.yaml": EMPTY_ROOT}
    if digests is not None:
        files["digests.json"] = json.dumps(digests).encode()
    files.update({f"flux/{name}.yaml": text.encode() for name, text in flux.items()})
    return files


def tarball(files: dict[str, bytes]) -> bytes:
    """The one gzipped layer `flux push artifact` writes, holding `files`."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, raw in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    return buffer.getvalue()


def green(name: str = CONTEXT) -> list[dict]:
    return [{"name": name, "status": "completed", "conclusion": "success"}]


def package_version(version_id: int, digest: str, tags, created: str) -> dict:
    """One entry of the Packages API's version listing, in the shape GitHub documents."""
    return {
        "id": version_id,
        "name": digest,
        "created_at": created,
        "updated_at": created,
        "metadata": {"package_type": "container", "container": {"tags": list(tags)}},
    }


@dataclass
class FakeTransport:
    """ghcr.io and api.github.com in one callable.

    Attributes:
        tags: each published tag and the digest it names.
        manifests: the manifest body a GET of each tag, or of each untagged digest, returns.
        check_runs: each commit's check-runs.
        forced: a status to answer for a manifest reference, overriding the tables.
        versions: the package versions the Packages API lists, newest first.
        api_forced: a status to answer for an api.github.com path, such as `.../versions`
            for the listing or `.../versions/<id>` for one delete.
        deleted: the id of each version a DELETE removed.
        blobs: each layer's bytes by its digest.
        statuses: each commit's newest status per context, as `{context: state}`.
        status_times: the `updated_at` every status on a commit carries; none when absent.
        requests: every `(method, url)` sent, in order.
    """

    tags: dict[str, str] = field(default_factory=dict)
    manifests: dict[str, dict] = field(default_factory=dict)
    check_runs: dict[str, list[dict]] = field(default_factory=dict)
    forced: dict[str, int] = field(default_factory=dict)
    versions: list[dict] = field(default_factory=list)
    api_forced: dict[str, int] = field(default_factory=dict)
    deleted: list[int] = field(default_factory=list)
    blobs: dict[str, bytes] = field(default_factory=dict)
    statuses: dict[str, dict[str, str]] = field(default_factory=dict)
    status_times: dict[str, str] = field(default_factory=dict)
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
        match = re.fullmatch(r"/repos/[^/]+/[^/]+/commits/(\w+)/status", parts.path)
        if (
            parts.netloc == "api.github.com"
            and match
            and parts.path not in self.api_forced
        ):
            found = self.statuses.get(match.group(1), {})
            stamp = {}
            if match.group(1) in self.status_times:
                stamp = {"updated_at": self.status_times[match.group(1)]}
            body = {
                "statuses": [
                    {"context": c, "state": s, **stamp} for c, s in found.items()
                ]
            }
            return forge.Response(200, body=json.dumps(body).encode())
        if parts.path in self.api_forced:
            message = {"message": f"forced HTTP {self.api_forced[parts.path]}"}
            body = json.dumps(message).encode()
            return forge.Response(self.api_forced[parts.path], body=body)
        listing = urlsplit(forge.PACKAGE_VERSIONS_URL).path
        if parts.path == listing and method == "GET":
            query = parse_qs(parts.query)
            size, page = int(query["per_page"][0]), int(query["page"][0])
            body = self.versions[(page - 1) * size : page * size]
            return forge.Response(200, body=json.dumps(body).encode())
        if parts.path.startswith(f"{listing}/") and method == "DELETE":
            version_id = int(parts.path.rsplit("/", 1)[1])
            if all(v["id"] != version_id for v in self.versions):
                return forge.Response(404)
            self.versions = [v for v in self.versions if v["id"] != version_id]
            self.deleted.append(version_id)
            return forge.Response(204)
        return forge.Response(404)

    def _registry(self, method, path):
        if path == "/token":
            return forge.Response(200, body=b'{"token": "registry-token"}')
        if "/blobs/" in path:
            blob = self.blobs.get(path.rsplit("/blobs/", 1)[1])
            return (
                forge.Response(404) if blob is None else forge.Response(200, body=blob)
            )
        if path.endswith("/tags/list"):
            return forge.Response(
                200, body=json.dumps({"tags": sorted(self.tags)}).encode()
            )
        reference = path.rsplit("/manifests/", 1)[1]
        if reference in self.forced:
            return forge.Response(self.forced[reference])
        if reference.startswith("sha256:") and reference in self.manifests:
            body = json.dumps(self.manifests[reference]).encode()
            return forge.Response(200, {"docker-content-digest": reference}, body)
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
        cosign_fails_for: the digests whose `cosign verify` exits 1.
        verified: each digest cosign was asked to verify.
    """

    hold: str | None = "none"
    hold_fails: bool = False
    cosign_fails: bool = False
    cosign_fails_for: set[str] = field(default_factory=set)
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
            digest = argv[-1].rsplit("@", 1)[1]
            self.verified.append(digest)
            if self.cosign_fails or digest in self.cosign_fails_for:
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
        # The pause before each package delete, recorded rather than slept.
        self.pauses: list[float] = []

    def commit(self, message: str) -> str:
        """Add an empty commit to master with `message`, append it to `commits`, return it."""
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", message)
        sha = _git(self.repo, "rev-parse", "HEAD")
        self.commits.append(sha)
        return sha

    def branch_commit(self) -> str:
        """A commit off master's root, on no path master's history takes."""
        _git(self.repo, "checkout", "-q", "-b", "side", self.commits[0])
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "side")
        sha = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "-q", "master")
        return sha

    def publish(
        self,
        sha: str,
        check_runs: list[dict] | None = None,
        files: dict[str, bytes] | None = None,
    ) -> None:
        """Push and sign `sha-<sha>`, with green CI unless `check_runs` says otherwise.

        Its bundle holds `files`, or the empty bundle when they are None.
        """
        self.transport.tags[f"sha-{sha}"] = digest_of(sha)
        self.transport.check_runs[sha] = green() if check_runs is None else check_runs
        self.store(digest_of(sha), files)

    def store(self, digest: str, files: dict[str, bytes] | None = None) -> None:
        """Make `digest` a pullable bundle holding `files`, the empty bundle by default."""
        layer = tarball(bundle_files(EMPTY_DIGESTS) if files is None else files)
        layer_digest = "sha256:" + hashlib.sha256(layer).hexdigest()
        self.transport.blobs[layer_digest] = layer
        self.transport.manifests[digest] = {"layers": [{"digest": layer_digest}]}

    def set_prod(
        self,
        sha: str,
        revision: str | None = None,
        files: dict[str, bytes] | None = None,
    ) -> None:
        """Point `prod` at `sha`, annotated with `revision` when given, holding `files`."""
        self.transport.tags["prod"] = digest_of(sha)
        value = f"master@sha1:{sha}" if revision is None else revision
        annotations = {promote.REVISION_ANNOTATION: value} if value else {}
        self.transport.manifests["prod"] = {"annotations": annotations}
        if files is not None or digest_of(sha) not in self.transport.manifests:
            self.store(digest_of(sha), files)

    def set_last_good(self, sha: str) -> None:
        """Point `last-good` at `sha-<sha>`'s digest."""
        self.transport.tags["last-good"] = digest_of(sha)

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
