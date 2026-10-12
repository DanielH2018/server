"""The forge adapter: every GitHub and GHCR call that `promote.py` makes.

`promote.py` decides which published master commit the cluster's `prod` tag moves to. Every
fact it reads from outside the checkout comes through this module:

- `Registry` speaks the OCI distribution API to ghcr.io: the token request, the manifest
  fetch and the manifest `HEAD`. It lists no tags, so a run's request count does not grow
  with the number of published versions.
- `GitHub` reads a repository variable through `gh variable get` and a commit's check-runs
  through the REST API.
- `Cosign` runs `cosign verify` under the identity the cluster's OCIRepository checks.

Each read returns a value, returns None for "absent", or raises `ForgeError`. What absent and
error MEAN for a promotion is `promote.py`'s business, not this module's. A forge move
rewrites this module and leaves `promote.py` alone.

Every HTTP request goes through a `Transport`, a callable taking the method, the URL and the
headers. The real one is `urllib_transport`. The tests pass a fake that models a registry and
counts the requests made.

The plan is `docs/target-architecture/deploy/d1-flux-installed.md`, *Decisions*,
*Every GitHub and GHCR call goes through one forge adapter*.
"""

import base64
import json
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

REPOSITORY = "DanielH2018/server"
REGISTRY = "ghcr.io"
ARTIFACT = "danielh2018/homelab-manifests"

# The keyless identity the `promote` workflow signs under, and the one the OCIRepository
# `homelab` checks. A signature from any other workflow or ref does not verify.
SIGNER_IDENTITY = "https://github.com/DanielH2018/server/.github/workflows/promote.yml@refs/heads/master"
SIGNER_ISSUER = "https://token.actions.githubusercontent.com"

# Seconds for one HTTP request or one subprocess. A timeout is an error, never an absence.
TIMEOUT_S = 30
# cosign verify fetches the signature, then reaches Sigstore's transparency log.
COSIGN_TIMEOUT_S = 120

USER_AGENT = "homelab-promote"

# The manifest types `flux push artifact` writes, plus the index and Docker types, so a HEAD
# answers with the digest of whatever the tag names rather than a 404 for an unlisted type.
MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class ForgeError(RuntimeError):
    """A forge read failed: a status that means neither present nor absent, or no answer."""


@dataclass(frozen=True)
class Response:
    """One HTTP response, whatever its status.

    Attributes:
        status: the HTTP status code.
        headers: the response headers, names lower-cased.
        body: the raw body, empty for a `HEAD`.
    """

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""


class Transport(Protocol):
    """Sends one request and returns its response, raising `ForgeError` on no answer."""

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str]
    ) -> Response: ...


def urllib_transport(method: str, url: str, headers: Mapping[str, str]) -> Response:
    """Send one request with `urllib`, returning a non-2xx status rather than raising it.

    Raises:
        ForgeError: the request timed out or never reached the server.
    """
    request = urllib.request.Request(
        url, method=method, headers={"User-Agent": USER_AGENT, **headers}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return Response(response.status, _lower(response.headers), response.read())
    except urllib.error.HTTPError as err:
        return Response(err.code, _lower(err.headers or {}), err.read() or b"")
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise ForgeError(f"{method} {url}: {err}") from err


def _lower(headers) -> dict[str, str]:
    return {name.lower(): value for name, value in headers.items()}


def _json(response: Response, what: str):
    try:
        return json.loads(response.body)
    except ValueError as err:
        raise ForgeError(f"{what}: the body is not JSON") from err


class Registry:
    """A read-only client for one repository on ghcr.io.

    It makes one token request per instance, on the first read, and caches the bearer token.
    """

    def __init__(
        self,
        transport: Transport,
        github_token: str | None,
        repository: str = ARTIFACT,
    ) -> None:
        """Holds the transport and the credential the token request presents.

        Args:
            transport: sends each request.
            github_token: the workflow's `GITHUB_TOKEN`, presented to GHCR's token endpoint.
                None or empty asks for an anonymous pull token, which a public package grants.
            repository: the `owner/name` of the package, lower-case as GHCR names it.
        """
        self._transport = transport
        self._github_token = github_token
        self.repository = repository
        self._bearer: str | None = None

    def _token(self) -> str:
        if self._bearer is not None:
            return self._bearer
        url = (
            f"https://{REGISTRY}/token?scope=repository:{self.repository}:pull"
            f"&service={REGISTRY}"
        )
        headers = {}
        if self._github_token:
            basic = base64.b64encode(f"token:{self._github_token}".encode()).decode()
            headers["Authorization"] = f"Basic {basic}"
        response = self._transport("GET", url, headers)
        if response.status != 200:
            raise ForgeError(f"GHCR token request: HTTP {response.status}")
        token = _json(response, "GHCR token request")
        if not isinstance(token, dict) or not isinstance(token.get("token"), str):
            raise ForgeError("GHCR token request: the body has no token")
        self._bearer = token["token"]
        return self._bearer

    def _manifest_request(self, method: str, reference: str) -> Response:
        url = f"https://{REGISTRY}/v2/{self.repository}/manifests/{reference}"
        headers = {
            "Accept": MANIFEST_ACCEPT,
            "Authorization": f"Bearer {self._token()}",
        }
        return self._transport(method, url, headers)

    def manifest(self, reference: str) -> dict | None:
        """The manifest `reference` names, parsed, or None when the registry has no such tag.

        HTTP 404, or any response carrying the registry error code `MANIFEST_UNKNOWN`, is
        absent.

        Raises:
            ForgeError: any other status, a body that is not a JSON object, or no answer.
        """
        response = self._manifest_request("GET", reference)
        if response.status == 200:
            manifest = _json(response, f"manifest {reference}")
            if not isinstance(manifest, dict):
                raise ForgeError(f"manifest {reference}: the body is not a JSON object")
            return manifest
        if response.status == 404 or "MANIFEST_UNKNOWN" in _error_codes(response):
            return None
        raise ForgeError(f"manifest {reference}: HTTP {response.status}")

    def digest(self, reference: str) -> str | None:
        """The digest of the manifest `reference` names, from one `HEAD`, or None on a 404.

        Raises:
            ForgeError: any other status, a 200 without a well-formed `Docker-Content-Digest`
                header, or no answer.
        """
        response = self._manifest_request("HEAD", reference)
        if response.status == 404:
            return None
        if response.status != 200:
            raise ForgeError(f"HEAD {reference}: HTTP {response.status}")
        digest = response.headers.get("docker-content-digest", "")
        if not _DIGEST.fullmatch(digest):
            raise ForgeError(
                f"HEAD {reference}: no sha256 Docker-Content-Digest header"
            )
        return digest


def _error_codes(response: Response) -> set[str]:
    """The `code` of each entry in an OCI error body, or nothing when the body has none."""
    try:
        body = json.loads(response.body)
    except ValueError:
        return set()
    errors = body.get("errors") if isinstance(body, dict) else None
    if not isinstance(errors, list):
        return set()
    return {str(e.get("code")) for e in errors if isinstance(e, dict)}


class GitHub:
    """Reads from one GitHub repository: a variable through `gh`, check-runs through REST."""

    def __init__(
        self,
        transport: Transport,
        token: str | None,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        repository: str = REPOSITORY,
    ) -> None:
        """Holds the two ways in.

        Args:
            transport: sends each REST request.
            token: the bearer token for REST reads. The variable read does not use it,
                because `gh` reads `GH_TOKEN` from its own environment.
            run: starts the `gh` subprocess.
            repository: the `owner/name` slug.
        """
        self._transport = transport
        self._token = token
        self._run = run
        self.repository = repository

    def variable(self, name: str) -> str | None:
        """The value of repository variable `name`, or None when the variable does not exist.

        The value comes back as `gh` prints it, less its one trailing newline.

        Raises:
            ForgeError: `gh` failed for any other reason, or did not finish.
        """
        argv = ["gh", "variable", "get", name, "-R", self.repository]
        try:
            proc = self._run(argv, capture_output=True, text=True, timeout=TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as err:
            raise ForgeError(f"gh variable get {name}: {err}") from err
        if proc.returncode == 0:
            return proc.stdout.removesuffix("\n")
        # cli/cli pkg/cmd/variable/get maps the API's 404 to this message.
        if f"variable {name} was not found" in proc.stderr:
            return None
        raise ForgeError(
            f"gh variable get {name} exited {proc.returncode}: {proc.stderr.strip()}"
        )

    def check_runs(self, sha: str) -> list[dict]:
        """Every check-run GitHub holds for commit `sha`, up to the API's page of 100.

        Raises:
            ForgeError: any status but 200, a body without a `check_runs` list, or no answer.
        """
        url = (
            f"https://api.github.com/repos/{self.repository}/commits/{sha}/check-runs"
            "?per_page=100"
        )
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        response = self._transport("GET", url, headers)
        if response.status != 200:
            raise ForgeError(f"check-runs for {sha}: HTTP {response.status}")
        body = _json(response, f"check-runs for {sha}")
        runs = body.get("check_runs") if isinstance(body, dict) else None
        if not isinstance(runs, list):
            raise ForgeError(f"check-runs for {sha}: the body has no check_runs list")
        return runs


class Cosign:
    """Runs `cosign verify` against the artifact's signer identity."""

    def __init__(
        self,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        repository: str = ARTIFACT,
    ) -> None:
        self._run = run
        self.repository = repository

    def verify(self, digest: str) -> None:
        """Verify the signature on the artifact version `digest` names.

        Raises:
            ForgeError: cosign exited non-zero or did not finish. A missing signature, a
                wrong identity and an unreachable Sigstore all land here, so the caller
                cannot mistake an outage for an unsigned version.
        """
        argv = [
            "cosign",
            "verify",
            "--certificate-identity",
            SIGNER_IDENTITY,
            "--certificate-oidc-issuer",
            SIGNER_ISSUER,
            f"{REGISTRY}/{self.repository}@{digest}",
        ]
        try:
            proc = self._run(
                argv, capture_output=True, text=True, timeout=COSIGN_TIMEOUT_S
            )
        except (OSError, subprocess.TimeoutExpired) as err:
            raise ForgeError(f"cosign verify {digest}: {err}") from err
        if proc.returncode != 0:
            raise ForgeError(
                f"cosign verify {digest} exited {proc.returncode}: {proc.stderr.strip()}"
            )


@dataclass(frozen=True)
class Forge:
    """The three clients `promote.py` reads through.

    Attributes:
        registry: the GHCR client for the artifact's repository.
        github: the repository's variable and check-runs reader.
        cosign: the signature check.
    """

    registry: Registry
    github: GitHub
    cosign: Cosign


def connect(
    env: Mapping[str, str],
    transport: Transport = urllib_transport,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Forge:
    """The live clients, with `GITHUB_TOKEN` from `env` for the registry and REST reads.

    `gh` reads `GH_TOKEN` from the environment it inherits, which the workflow sets to
    `PROMOTE_TOKEN` on the step that runs `promote.py decide`.
    """
    token = env.get("GITHUB_TOKEN") or None
    return Forge(
        registry=Registry(transport, token),
        github=GitHub(transport, token, run=run),
        cosign=Cosign(run=run),
    )
