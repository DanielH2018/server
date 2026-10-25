"""The GHCR half of `forge.py`: the OCI distribution reads `promote.py` makes of ghcr.io.

`forge.py` re-exports `Registry`, `REGISTRY`, `ARTIFACT` and `MANIFEST_ACCEPT`, so a caller
names them as `forge.<name>` and never imports this module. `Registry` moved here when
`forge.py` reached its length cap and the check-runs reader needed room to page (#5977).
"""

import base64
import gzip
import io
import re
import tarfile
from collections.abc import Mapping

from forge_transport import ForgeError, Response, Transport, _error_codes, _json

REGISTRY = "ghcr.io"
ARTIFACT = "danielh2018/homelab-manifests"

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
