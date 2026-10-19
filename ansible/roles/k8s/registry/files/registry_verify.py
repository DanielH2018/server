"""registry's verify Jobs: push, pull through containerd, and prove the fence refuses others.

`templates/verify-inert/` holds the Jobs, which no render emits while Ansible owns registry
(target slice D5, *A verify conversion lands as inert code*). They replace the checks
`tasks/main.yml` runs on every deploy. `CHECK` in the Job's environment picks one:

- `push` replaces `registry-selftest-push`. It pushes a fixed two-blob image to
  `REGISTRY_URL` as `<REPO>:<TAG>` through the distribution API, then reads the tag back and
  compares the digest the registry reports with the manifest it sent. The bytes never change,
  so a re-run stores nothing new: each blob that is already present is skipped, and the
  manifest Put re-points the tag at the same digest. The pod carries the selftest's
  `app: registry-selftest-push` label, which the registry's fence already admits.

  It then seeds the image the `pull` Jobs run. `SOURCE_IMAGE` is the fleet's python Job
  image, pinned by digest, and the push copies its `PLATFORM` manifest and blobs from its
  upstream registry into `<REPO>:<SEED_TAG>`. `SEED_TAG` names the source digest, so a tag the
  store already serves is a seed already made and costs one request. Only a new pin reads the
  upstream registry, and only blobs the store lacks cross the WAN.
- `pull` replaces `registry-selftest-pull` and `registry-selftest-pull-agent`. Its pod runs
  the seeded copy from `localhost:5000`, so the proof is that the pod started at all: the
  node's containerd resolved the mirror entry in registries.yaml, spoke plain HTTP to the
  registry and got the layers. The script only reports `PULLED_IMAGE`, and opens no
  connection.
- `isolation` replaces `registry-netpol-probe`. Every URL in `ISOLATED_URLS` must refuse the
  connect or time out, which `flux_job_connect.refused` decides. Any answer fails the leg, an
  HTTP error status included. Any other error fails it too, because it proves nothing about
  the policy, so a pod whose DNS lookups fail fails here. That covers the old probe's
  `getent hosts registry` control. The pod carries `app: registry-verify`, which the fence
  must never admit.

Inputs come from the Job's environment. `REGISTRY_RENDER_DIGEST8` is in the spec only to rename
the Job, and the script prints it. Exit 0 when the check passes, 1 when it fails, naming why.

Run: python3 registry_verify.py (in the Job; it takes no arguments)
     uv run pytest ansible/roles/k8s/registry/tests/test_registry_verify.py
     uv run pytest ansible/roles/k8s/registry/tests/test_registry_verify_seed.py
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
from collections.abc import Mapping

from flux_job_connect import Request, open_url, refused

# The socket timeout of each request, in seconds; the probe it replaces used `nc -w 5`.
TIMEOUT = 5
# How long the push's first request retries a refused connect, in seconds: the helper's
# default, while kube-router learns the new pod's address.
PUSH_RETRY_SECONDS = 60

# The image the push writes. An uncompressed empty tar is a valid OCI layer, and fixed bytes
# give fixed digests, so the store holds one copy however often the Job runs.
LAYER = bytes(1024)
LAYER_TYPE = "application/vnd.oci.image.layer.v1.tar"
CONFIG_TYPE = "application/vnd.oci.image.config.v1+json"
MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"
# The manifest kinds a seed reads: an index or list names one manifest per platform.
INDEX_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
IMAGE_TYPES = (MANIFEST_TYPE, "application/vnd.docker.distribution.manifest.v2+json")
# The socket timeout of each request that moves a seed blob, in seconds.
BLOB_TIMEOUT = 60
# Where an image reference with no registry host is served from.
DOCKER_HUB = "registry-1.docker.io"


class Failed(Exception):
    """The check failed; the message names what is wrong."""


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def image() -> tuple[list[tuple[bytes, str]], bytes]:
    """The probe image: its blobs with their media types, and its manifest's bytes."""
    config = json.dumps(
        {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": [_digest(LAYER)]},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": MANIFEST_TYPE,
            "config": {
                "mediaType": CONFIG_TYPE,
                "digest": _digest(config),
                "size": len(config),
            },
            "layers": [
                {"mediaType": LAYER_TYPE, "digest": _digest(LAYER), "size": len(LAYER)}
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return [(LAYER, LAYER_TYPE), (config, CONFIG_TYPE)], manifest


def _close(err: BaseException) -> None:
    """Close the response an `HTTPError` carries, which nothing else here reads."""
    if isinstance(err, urllib.error.HTTPError):
        err.close()


def _call(request: Request, want: int, retry_seconds: float = 0):
    """Send `request` and return its response, which must carry status `want`.

    Raises:
        Failed: The registry answered another status, or could not be reached.
    """
    try:
        response = open_url(request, timeout=TIMEOUT, retry_seconds=retry_seconds)
    except urllib.error.HTTPError as err:
        _close(err)
        raise Failed(
            f"{request.get_method()} {request.full_url} answered {err.code}, "
            f"expected {want}"
        ) from err
    except OSError as err:
        hint = (
            " Check the Service, and that the registry fence still admits "
            "app=registry-selftest-push."
            if refused(err)
            else ""
        )
        raise Failed(f"cannot reach {request.full_url} ({err}).{hint}") from err
    if response.status != want:
        response.close()
        raise Failed(
            f"{request.get_method()} {request.full_url} answered {response.status}, "
            f"expected {want}"
        )
    return response


def _blob_present(base: str, repo: str, digest: str, retry_seconds: float) -> bool:
    url = f"{base}/v2/{repo}/blobs/{digest}"
    try:
        with open_url(
            Request(url, method="HEAD"), timeout=TIMEOUT, retry_seconds=retry_seconds
        ):
            return True
    except urllib.error.HTTPError as err:
        _close(err)
        if err.code == 404:
            return False
        raise Failed(f"HEAD {url} answered {err.code}, expected 200 or 404") from err
    except OSError as err:
        raise Failed(
            f"cannot reach {url} ({err}). Check the Service, and that the registry fence "
            "still admits app=registry-selftest-push."
        ) from err


def push_blob(base: str, repo: str, data: bytes, retry_seconds: float = 0) -> str:
    """Upload `data` to `repo` unless the registry holds it already; return a report line."""
    digest = _digest(data)
    if _blob_present(base, repo, digest, retry_seconds):
        return f"blob {digest} already present"
    with _call(
        Request(f"{base}/v2/{repo}/blobs/uploads/", data=b"", method="POST"), 202
    ) as started:
        location = started.headers.get("Location")
    if not location:
        raise Failed(f"POST {base}/v2/{repo}/blobs/uploads/ returned no Location")
    url = urllib.parse.urljoin(f"{base}/", location)
    url += ("&" if urllib.parse.urlsplit(url).query else "?") + urllib.parse.urlencode(
        {"digest": digest}
    )
    request = Request(
        url,
        data=data,
        method="PUT",
        headers={"Content-Type": "application/octet-stream"},
    )
    with _call(request, 201):
        pass
    return f"pushed blob {digest}"


def push(
    base: str, repo: str, tag: str, retry_seconds: float = PUSH_RETRY_SECONDS
) -> list[str]:
    """Push the probe image to `base` as `repo:tag`, and read the tag back.

    Args:
        base: The registry's URL over its Service, with no trailing slash.
        repo: The repository to push to.
        tag: The tag to point at the manifest.
        retry_seconds: How long the first request retries a refused connect.

    Raises:
        Failed: A request failed, or the tag reads back as another digest.
    """
    blobs, manifest = image()
    lines = [
        push_blob(base, repo, data, retry_seconds if n == 0 else 0)
        for n, (data, _) in enumerate(blobs)
    ]
    url = f"{base}/v2/{repo}/manifests/{tag}"
    with _call(
        Request(
            url, data=manifest, method="PUT", headers={"Content-Type": MANIFEST_TYPE}
        ),
        201,
    ):
        pass
    with _call(Request(url, headers={"Accept": MANIFEST_TYPE}), 200) as response:
        served = response.headers.get("Docker-Content-Digest")
        body = response.read()
    want = _digest(manifest)
    if served != want or body != manifest:
        raise Failed(
            f"{repo}:{tag} reads back as {served}, expected {want}: the registry did not "
            "store what was pushed"
        )
    lines.append(f"pushed and read back {repo}:{tag} as {want}")
    return lines


def parse_image(ref: str) -> tuple[str, str, str]:
    """Split `[host/]name[:tag]@sha256:<hex>` into its registry URL, repository and digest.

    Raises:
        Failed: `ref` is not pinned by digest, so no seed could be checked against it.
    """
    name, sep, digest = ref.partition("@")
    if not sep or not digest.startswith("sha256:"):
        raise Failed(f"SOURCE_IMAGE {ref!r} is not pinned by a sha256 digest")
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        host, path = first, rest
    else:
        host, path = DOCKER_HUB, name
    if host in ("docker.io", "index.docker.io"):
        host = DOCKER_HUB
    if host == DOCKER_HUB and "/" not in path:
        path = "library/" + path
    repo = path.split(":", 1)[0]
    # A loopback registry speaks plain HTTP, as docker assumes of one.
    loopback = host.split(":")[0] in ("localhost", "127.0.0.1")
    return f"{'http' if loopback else 'https'}://{host}", repo, digest


class Upstream:
    """Reads one repository of a source registry, with a bearer token once it asks for one.

    The token rides as an unredirected header, so a blob GET that the registry redirects to its
    CDN does not carry it there.
    """

    def __init__(self, base: str, repo: str):
        self.base = base
        self.repo = repo
        self.token: str | None = None

    def _request(self, path: str, accept: str = "") -> Request:
        request = Request(f"{self.base}/v2/{self.repo}/{path}")
        if accept:
            request.add_header("Accept", accept)
        if self.token:
            request.add_unredirected_header("Authorization", f"Bearer {self.token}")
        return request

    def _login(self, challenge: str) -> None:
        scheme, _, params = challenge.partition(" ")
        fields = dict(
            (key.strip(), value.strip().strip('"'))
            for key, _, value in (p.partition("=") for p in params.split(","))
        )
        if scheme.lower() != "bearer" or "realm" not in fields:
            raise Failed(
                f"{self.base} asks for {challenge!r}, which is no bearer login"
            )
        query = {k: v for k, v in fields.items() if k in ("service", "scope")}
        url = fields["realm"] + "?" + urllib.parse.urlencode(query)
        with _call(Request(url), 200) as response:
            body = json.loads(response.read())
        self.token = body.get("token") or body.get("access_token")
        if not self.token:
            raise Failed(f"{fields['realm']} returned no token")

    def _open(self, path: str, accept: str, timeout: float):
        try:
            return open_url(
                self._request(path, accept), timeout=timeout, retry_seconds=0
            )
        except urllib.error.HTTPError:
            raise
        except OSError as err:
            raise Failed(
                f"cannot reach {self.base} ({err}) to seed the pull image"
            ) from err

    def open(self, path: str, accept: str = "", timeout: float = TIMEOUT):
        """GET `path` under the repository, logging in once if the registry answers 401.

        Raises:
            Failed: The registry answered another error, or a 401 after the login.
        """
        url = f"{self.base}/v2/{self.repo}/{path}"
        try:
            return self._open(path, accept, timeout)
        except urllib.error.HTTPError as err:
            challenge = err.headers.get("WWW-Authenticate", "")
            _close(err)
            if err.code != 401 or self.token:
                raise Failed(f"GET {url} answered {err.code}") from err
        self._login(challenge)
        try:
            return self._open(path, accept, timeout)
        except urllib.error.HTTPError as err:
            _close(err)
            raise Failed(f"GET {url} answered {err.code} after the login") from err

    def manifest(self, digest: str) -> tuple[str, bytes]:
        """The manifest `digest` names, checked against it, and its media type."""
        accept = ",".join((*INDEX_TYPES, *IMAGE_TYPES))
        with self.open(f"manifests/{digest}", accept) as response:
            body = response.read()
            media = response.headers.get("Content-Type", "")
        if _digest(body) != digest:
            raise Failed(f"{self.repo}@{digest} served bytes of another digest")
        return json.loads(body).get("mediaType") or media, body


def platform_digest(index: bytes, platform: str) -> str:
    """The digest of the manifest `index` names for `platform`, as `os/arch`."""
    want_os, _, want_arch = platform.partition("/")
    for entry in json.loads(index).get("manifests", []):
        found = entry.get("platform") or {}
        if (found.get("os"), found.get("architecture")) == (want_os, want_arch):
            return entry["digest"]
    raise Failed(f"the source index names no {platform} manifest")


def _tag_served(base: str, repo: str, tag: str) -> bool:
    url = f"{base}/v2/{repo}/manifests/{tag}"
    try:
        with open_url(
            Request(url, method="HEAD", headers={"Accept": ",".join(IMAGE_TYPES)}),
            timeout=TIMEOUT,
            retry_seconds=0,
        ):
            return True
    except urllib.error.HTTPError as err:
        _close(err)
        if err.code == 404:
            return False
        raise Failed(f"HEAD {url} answered {err.code}, expected 200 or 404") from err
    except OSError as err:
        raise Failed(f"cannot reach {url} ({err})") from err


def _copy_blob(upstream: Upstream, base: str, repo: str, blob: dict) -> str:
    """Stream one blob from `upstream` into the store, unless the store holds it already."""
    digest = blob["digest"]
    if _blob_present(base, repo, digest, 0):
        return f"seed blob {digest} already present"
    with _call(
        Request(f"{base}/v2/{repo}/blobs/uploads/", data=b"", method="POST"), 202
    ) as started:
        location = started.headers.get("Location")
    if not location:
        raise Failed(f"POST {base}/v2/{repo}/blobs/uploads/ returned no Location")
    url = urllib.parse.urljoin(f"{base}/", location)
    url += ("&" if urllib.parse.urlsplit(url).query else "?") + urllib.parse.urlencode(
        {"digest": digest}
    )
    with upstream.open(f"blobs/{digest}", timeout=BLOB_TIMEOUT) as source:
        # The body streams from the upstream response, so the pod never holds a whole layer.
        # The store refuses a body whose bytes miss `digest`.
        request = Request(
            url,
            data=source,
            method="PUT",
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(blob["size"]),
            },
        )
        with open_url(request, timeout=BLOB_TIMEOUT, retry_seconds=0) as response:
            if response.status != 201:
                raise Failed(f"PUT {url} answered {response.status}, expected 201")
    return f"seeded blob {digest}"


def seed(source: str, platform: str, base: str, repo: str, tag: str) -> list[str]:
    """Copy `source`'s `platform` image into `base` as `repo:tag`, unless it is there already.

    Raises:
        Failed: The store or the source registry failed a request, or the source served
            bytes that miss their digest.
    """
    if _tag_served(base, repo, tag):
        return [f"seed {repo}:{tag} already served"]
    source_base, source_repo, digest = parse_image(source)
    upstream = Upstream(source_base, source_repo)
    media, body = upstream.manifest(digest)
    if media in INDEX_TYPES:
        media, body = upstream.manifest(platform_digest(body, platform))
    if media not in IMAGE_TYPES:
        raise Failed(f"{source} resolves to a {media!r}, which is no image manifest")
    manifest = json.loads(body)
    lines = [
        _copy_blob(upstream, base, repo, blob)
        for blob in (manifest["config"], *manifest["layers"])
    ]
    url = f"{base}/v2/{repo}/manifests/{tag}"
    with _call(
        Request(url, data=body, method="PUT", headers={"Content-Type": media}), 201
    ):
        pass
    with _call(Request(url, headers={"Accept": media}), 200) as response:
        served = response.headers.get("Docker-Content-Digest")
    if served != _digest(body):
        raise Failed(f"{repo}:{tag} reads back as {served}, expected {_digest(body)}")
    lines.append(f"seeded {repo}:{tag} as {served} from {source} ({platform})")
    return lines


def isolated(url: str) -> str:
    """Prove the connect to `url` is refused or times out.

    Raises:
        Failed: The registry accepted the connection, or the connect failed for a reason
            that is no refusal, such as a DNS error.
    """
    try:
        with open_url(url, timeout=TIMEOUT, retry_seconds=0):
            pass
    except Exception as err:
        _close(err)
        if refused(err):
            return f"isolated: {url} refused, as intended"
        raise Failed(
            f"no refusal from {url}: {err!r}. Either the registry accepted the connection "
            "from a pod its fence does not admit, so NetworkPolicy registry is not taking "
            "effect, or the connect failed for another reason, which proves nothing."
        ) from err
    raise Failed(
        f"POLICY NOT ENFORCED: {url} answered a pod with neither "
        "netpol-role=image-builder nor an admitted selftest label. The registry is open "
        "to the whole namespace."
    )


def run(
    env: Mapping[str, str], push_retry_seconds: float = PUSH_RETRY_SECONDS
) -> list[str]:
    """Run the check `CHECK` names and return its report lines.

    Raises:
        Failed: The check failed, or `CHECK` names no check.
    """
    lines = [
        f"verifying registry's render {env.get('REGISTRY_RENDER_DIGEST8', 'unknown')}"
    ]
    check = env.get("CHECK")
    if check == "push":
        base = env["REGISTRY_URL"].rstrip("/")
        lines += push(base, env["REPO"], env["TAG"], push_retry_seconds)
        return lines + seed(
            env["SOURCE_IMAGE"], env["PLATFORM"], base, env["REPO"], env["SEED_TAG"]
        )
    if check == "pull":
        image = env.get("PULLED_IMAGE", "")
        if not image:
            raise Failed("PULLED_IMAGE is empty, so the report would name nothing")
        return lines + [
            f"pulled {image}: this node's containerd resolved the push store through "
            "registries.yaml"
        ]
    if check == "isolation":
        urls = env.get("ISOLATED_URLS", "").split()
        if not urls:
            raise Failed("ISOLATED_URLS names no URL, so nothing would be checked")
        return lines + [isolated(url) for url in urls]
    raise Failed(f"CHECK is {check!r}; expected 'push', 'pull' or 'isolation'")


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    try:
        lines = run(os.environ if env is None else env)
    except Failed as err:
        print(f"registry-verify: {err}", file=sys.stderr)
        return 1
    for line in lines:
        print(f"registry-verify: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
