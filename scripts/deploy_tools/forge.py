"""The forge adapter: every GitHub and GHCR call that `promote.py` makes.

`promote.py` decides which published master commit the cluster's `prod` tag moves to. Every
fact it reads from outside the checkout comes through this module:

- `Registry` speaks the OCI distribution API to ghcr.io: the token request, the manifest
  fetch and the manifest `HEAD`, and a bundle's files read out of its layer, for `decide`'s
  pulls and `land-wait`. It lists no tags, so a run's request count does not grow with the
  number of published versions.
- `GitHub` reads a repository variable through `gh variable get`, and a commit's check-runs,
  commit statuses and pull requests through the REST API. It writes one variable, DEPLOY_HOLD,
  through `gh variable set`, for `decide`'s forward-only gate hold. For `promote.py prune` it also lists the
  artifact's package versions and deletes one, through the Packages API.
- `Cosign` runs `cosign verify` under the identity the cluster's OCIRepository checks.
- `Discord` posts `promote.py hold`'s messages to the deployer's channel, through the webhook
  in the `DEPLOY_DISCORD_WEBHOOK` repository secret. It never prints the URL.

Each read returns a value, returns None for "absent", or raises `ForgeError`. What absent and
error MEAN for a promotion is `promote.py`'s business, not this module's. A forge move
rewrites this module and leaves `promote.py` alone.

Every HTTP request goes through a `Transport`, a callable taking the method, the URL and the
headers: `urllib_transport`, or the tests' fake, which models a registry and counts requests.

The plan is `docs/target-architecture/deploy/d1-flux-installed.md`, *Decisions*,
*Every GitHub and GHCR call goes through one forge adapter*.
"""

import base64
import gzip
import io
import re
import subprocess
import tarfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from forge_transport import (
    TIMEOUT_S,
    ForgeError,
    Response,
    Transport,
    _api_message,
    _error_codes,
    _json,
    _updated,
    urllib_post as urllib_post,
    urllib_transport,
)
from forge_transport import DISCORD_MAX as DISCORD_MAX, Discord as Discord


REPOSITORY = "DanielH2018/server"
# The repository variable that holds promotion: `none`, or why it is held (contract C2).
HOLD_VARIABLE = "DEPLOY_HOLD"
REGISTRY = "ghcr.io"
ARTIFACT = "danielh2018/homelab-manifests"

# The artifact as the Packages API names it: a container package owned by the user account,
# not by the repository, so its path starts at /users.
PACKAGE_VERSIONS_URL = "https://api.github.com/users/DanielH2018/packages/container/homelab-manifests/versions"
# The API's largest page. A listing longer than VERSION_PAGES_MAX pages is an error rather than
# a truncation, because `prune` reads a digest missing from the listing as deleted.
VERSIONS_PER_PAGE = 100
VERSION_PAGES_MAX = 300
# Seconds between two deletes. GitHub's REST best practices ask for at least one second between
# mutating requests, and a burst trips the secondary rate limit as a 403 that reads like a
# permission error.
DELETE_PAUSE_S = 1.0

# The keyless identity the `promote` workflow signs under, and the one the OCIRepository
# `homelab` checks. A signature from any other workflow or ref does not verify.
SIGNER_IDENTITY = "https://github.com/DanielH2018/server/.github/workflows/promote.yml@refs/heads/master"
SIGNER_ISSUER = "https://token.actions.githubusercontent.com"

# cosign verify fetches the signature, then reaches Sigstore's transparency log.
COSIGN_TIMEOUT_S = 120

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

    def _request(
        self, method: str, url: str, headers: Mapping[str, str] | None = None
    ) -> Response:
        """One registry request with the bearer token, asking for a new token once on a 401.

        GHCR's pull token expires after a few minutes, and `promote.py land-wait` holds one
        Registry for its whole wait, so a 401 means "ask again" before it means "refused".
        """
        response = self._transport(
            method, url, {**(headers or {}), "Authorization": f"Bearer {self._token()}"}
        )
        if response.status == 401:
            self._bearer = None
            response = self._transport(
                method,
                url,
                {**(headers or {}), "Authorization": f"Bearer {self._token()}"},
            )
        return response

    def _manifest_request(self, method: str, reference: str) -> Response:
        url = f"https://{REGISTRY}/v2/{self.repository}/manifests/{reference}"
        return self._request(method, url, {"Accept": MANIFEST_ACCEPT})

    def manifest(self, reference: str) -> dict | None:
        """The manifest `reference` names, parsed, or None when the registry has no such tag.

        HTTP 404, or any response carrying the registry error code `MANIFEST_UNKNOWN`, is
        absent.

        Raises:
            ForgeError: any other status, a body that is not a JSON object, or no answer.
        """
        found = self._manifest(reference)
        return None if found is None else found[0]

    def tagged(self, reference: str) -> tuple[dict, str] | None:
        """The manifest `reference` names and its digest, from one GET, or None when absent.

        The digest is the response's `Docker-Content-Digest`, so reading a tag's commit and
        its digest costs one request rather than a GET and a HEAD.

        Raises:
            ForgeError: as `manifest`, or a 200 without a well-formed digest header.
        """
        found = self._manifest(reference)
        if found is None:
            return None
        manifest, response = found
        digest = response.headers.get("docker-content-digest", "")
        if not _DIGEST.fullmatch(digest):
            raise ForgeError(
                f"manifest {reference}: no sha256 Docker-Content-Digest header"
            )
        return manifest, digest

    def _manifest(self, reference: str) -> tuple[dict, Response] | None:
        response = self._manifest_request("GET", reference)
        if response.status == 200:
            manifest = _json(response, f"manifest {reference}")
            if not isinstance(manifest, dict):
                raise ForgeError(f"manifest {reference}: the body is not a JSON object")
            return manifest, response
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

    def blob(self, digest: str) -> bytes:
        """The bytes of the blob `digest` names.

        GHCR answers with a redirect to its content host, which `urllib` follows.

        Raises:
            ForgeError: any status but 200, or no answer. A listed layer that is absent is
                an error, because its manifest names it.
        """
        url = f"https://{REGISTRY}/v2/{self.repository}/blobs/{digest}"
        response = self._request("GET", url)
        if response.status != 200:
            raise ForgeError(f"blob {digest}: HTTP {response.status}")
        return response.body

    def bundle(self, reference: str) -> dict[str, bytes] | None:
        """Every regular file in the bundle `reference` names, by its path from the bundle root.

        `flux push artifact` writes one layer, a gzipped tarball of the bundle directory, so
        the files are read out of that layer. `reference` is a tag or a `sha256:` digest.

        Returns:
            The files, or None when the registry has no such tag or digest.

        Raises:
            ForgeError: the manifest has no single layer, or the layer is not a gzipped
                tarball. Each read of the registry raises as `manifest` and `blob` do.
        """
        manifest = self.manifest(reference)
        if manifest is None:
            return None
        layers = manifest.get("layers")
        layer = layers[0] if isinstance(layers, list) and len(layers) == 1 else None
        digest = layer.get("digest") if isinstance(layer, dict) else None
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ForgeError(f"manifest {reference}: not one layer with a digest")
        files: dict[str, bytes] = {}
        try:
            with tarfile.open(
                fileobj=io.BytesIO(self.blob(digest)), mode="r:gz"
            ) as tar:
                for member in tar.getmembers():
                    handle = tar.extractfile(member) if member.isfile() else None
                    if handle is not None:
                        files[member.name.removeprefix("./")] = handle.read()
        except (tarfile.TarError, gzip.BadGzipFile, EOFError, OSError) as err:
            raise ForgeError(f"bundle {reference}: {err}") from err
        return files


class GitHub:
    """One GitHub repository: a variable through `gh`, check-runs and packages through REST."""

    def __init__(
        self,
        transport: Transport,
        token: str | None,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        repository: str = REPOSITORY,
        versions_per_page: int = VERSIONS_PER_PAGE,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Holds the two ways in.

        Args:
            transport: sends each REST request.
            token: the bearer token for REST calls. The variable read does not use it,
                because `gh` reads `GH_TOKEN` from its own environment.
            run: starts the `gh` subprocess.
            repository: the `owner/name` slug.
            versions_per_page: the page size of the package listing.
            sleep: waits DELETE_PAUSE_S before each delete.
        """
        self._transport = transport
        self._token = token
        self._run = run
        self.repository = repository
        self._versions_per_page = versions_per_page
        self._sleep = sleep

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
        response = self._transport("GET", url, self._rest_headers())
        if response.status != 200:
            raise ForgeError(f"check-runs for {sha}: HTTP {response.status}")
        body = _json(response, f"check-runs for {sha}")
        runs = body.get("check_runs") if isinstance(body, dict) else None
        if not isinstance(runs, list):
            raise ForgeError(f"check-runs for {sha}: the body has no check_runs list")
        return runs

    def statuses(self, sha: str, since: datetime | None = None) -> dict[str, str]:
        """The newest commit status per context on commit `sha`, as `{context: state}`.

        Reads GitHub's combined status, which keeps the newest status of each context. A
        state is `pending`, `success`, `failure` or `error`. A context with no status is
        absent from the map, which the caller must never read as success. With `since`, a
        status whose `updated_at` precedes it, or is unreadable, is absent too.

        Raises:
            ForgeError: any status but 200, a body without a `statuses` list, or no answer.
        """
        url = (
            f"https://api.github.com/repos/{self.repository}/commits/{sha}/status"
            "?per_page=100"
        )
        response = self._transport("GET", url, self._rest_headers())
        if response.status != 200:
            raise ForgeError(
                f"statuses for {sha}: HTTP {response.status}{_api_message(response)}"
            )
        body = _json(response, f"statuses for {sha}")
        found = body.get("statuses") if isinstance(body, dict) else None
        if not isinstance(found, list):
            raise ForgeError(f"statuses for {sha}: the body has no statuses list")
        return {
            entry["context"]: entry["state"]
            for entry in found
            if isinstance(entry, dict)
            and isinstance(entry.get("context"), str)
            and isinstance(entry.get("state"), str)
            and (since is None or _updated(entry) >= since)
        }

    def status(self, sha: str, context: str) -> tuple[str, datetime] | None:
        """The newest status of `context` on commit `sha`, as `(state, updated_at)`.

        Returns:
            None when the commit carries no status of that context. An `updated_at` that is
            missing or malformed reads as the earliest time, so an age bound refuses it.

        Raises:
            ForgeError: as `statuses` does.
        """
        url = (
            f"https://api.github.com/repos/{self.repository}/commits/{sha}/status"
            "?per_page=100"
        )
        response = self._transport("GET", url, self._rest_headers())
        if response.status != 200:
            raise ForgeError(
                f"statuses for {sha}: HTTP {response.status}{_api_message(response)}"
            )
        body = _json(response, f"statuses for {sha}")
        found = body.get("statuses") if isinstance(body, dict) else None
        if not isinstance(found, list):
            raise ForgeError(f"statuses for {sha}: the body has no statuses list")
        for entry in found:
            if isinstance(entry, dict) and entry.get("context") == context:
                return str(entry.get("state")), _updated(entry)
        return None

    def commit_pulls(self, sha: str) -> list[dict]:
        """The pull requests GitHub associates with commit `sha`, up to a page of 100.

        Raises:
            ForgeError: any status but 200, a body that is not a JSON list, or no answer.
        """
        url = (
            f"https://api.github.com/repos/{self.repository}/commits/{sha}/pulls"
            "?per_page=100"
        )
        response = self._transport("GET", url, self._rest_headers())
        if response.status != 200:
            raise ForgeError(
                f"pull requests for {sha}: HTTP {response.status}{_api_message(response)}"
            )
        body = _json(response, f"pull requests for {sha}")
        if not isinstance(body, list):
            raise ForgeError(f"pull requests for {sha}: the body is not a list")
        return body

    def set_variable(self, name: str, value: str) -> None:
        """Set repository variable `name` to `value` through `gh`, which reads `GH_TOKEN`.

        Raises:
            ForgeError: `gh` failed, or did not finish.
        """
        argv = ["gh", "variable", "set", name, "--body", value, "-R", self.repository]
        try:
            proc = self._run(argv, capture_output=True, text=True, timeout=TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as err:
            raise ForgeError(f"gh variable set {name}: {err}") from err
        if proc.returncode != 0:
            raise ForgeError(
                f"gh variable set {name} exited {proc.returncode}: {proc.stderr.strip()}"
            )

    def package_versions(self) -> list[dict]:
        """Every version of the artifact's package, as the Packages API returns each one.

        Reads page after page until one comes back empty, so the list is the whole listing or
        an error, never a prefix of it.

        Raises:
            ForgeError: any status but 200 on any page, a page that is not a JSON list, more
                than VERSION_PAGES_MAX pages, or no answer.
        """
        versions = []
        for page in range(1, VERSION_PAGES_MAX + 1):
            url = (
                f"{PACKAGE_VERSIONS_URL}?per_page={self._versions_per_page}&page={page}"
            )
            response = self._transport("GET", url, self._rest_headers())
            if response.status != 200:
                raise ForgeError(
                    f"package versions page {page}: HTTP {response.status}"
                    f"{_api_message(response)}"
                )
            body = _json(response, f"package versions page {page}")
            if not isinstance(body, list):
                raise ForgeError(
                    f"package versions page {page}: the body is not a list"
                )
            if not body:
                return versions
            versions.extend(body)
        raise ForgeError(
            f"package versions: more than {VERSION_PAGES_MAX} pages of "
            f"{self._versions_per_page}"
        )

    def delete_package_version(self, version_id: int) -> bool:
        """Delete one version of the artifact's package.

        Returns:
            True when the version was deleted, False when the API no longer holds it.

        Raises:
            ForgeError: any status but 204 or 404, or no answer. A token without the
                package's Admin role gets a 403 here.
        """
        url = f"{PACKAGE_VERSIONS_URL}/{version_id}"
        self._sleep(DELETE_PAUSE_S)
        response = self._transport("DELETE", url, self._rest_headers())
        if response.status == 204:
            return True
        if response.status == 404:
            return False
        raise ForgeError(
            f"delete package version {version_id}: HTTP {response.status}"
            f"{_api_message(response)}"
        )

    def _rest_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers


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
    """The live clients, with `GITHUB_TOKEN` from `env` for the registry and REST calls.

    `gh` reads `GH_TOKEN` from the environment it inherits, which the workflow sets to
    `PROMOTE_TOKEN` on the step that runs `promote.py decide`.
    """
    token = env.get("GITHUB_TOKEN") or None
    return Forge(
        registry=Registry(transport, token),
        github=GitHub(transport, token, run=run),
        cosign=Cosign(run=run),
    )
