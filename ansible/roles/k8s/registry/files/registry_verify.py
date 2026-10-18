"""registry's verify Jobs: prove a pod can push to the registry, and that the fence refuses others.

`templates/verify-inert/` holds the two Jobs, which no render emits while Ansible owns registry
(target slice D5, *A verify conversion lands as inert code*). They replace two of the checks
`tasks/main.yml` runs on every deploy. `CHECK` in the Job's environment picks one:

- `push` replaces `registry-selftest-push`. It pushes a fixed two-blob image to
  `REGISTRY_URL` as `<REPO>:<TAG>` through the distribution API, then reads the tag back and
  compares the digest the registry reports with the manifest it sent. The bytes never change,
  so a re-run stores nothing new: each blob that is already present is skipped, and the
  manifest Put re-points the tag at the same digest. The pod carries the selftest's
  `app: registry-selftest-push` label, which the registry's fence already admits.
- `isolation` replaces `registry-netpol-probe`. Every URL in `ISOLATED_URLS` must refuse the
  connect or time out, which `flux_job_connect.refused` decides. Any answer fails the leg, an
  HTTP error status included. Any other error fails it too, because it proves nothing about
  the policy, so a pod whose DNS lookups fail fails here. That covers the old probe's
  `getent hosts registry` control. The pod carries `app: registry-verify`, which the fence
  must never admit.

The pull self-tests have no Job form, because their proof is the pod's own image pulling from
`localhost:5000`. The Job census requires every container to run `python3` on a mounted
script, and the probe image holds no Python. `templates/verify-inert/job.yaml.j2` says so.

Inputs come from the Job's environment. `REGISTRY_RENDER_DIGEST8` is in the spec only to rename
the Job, and the script prints it. Exit 0 when the check passes, 1 when it fails, naming why.

Run: python3 registry_verify.py (in the Job; it takes no arguments)
     uv run pytest ansible/roles/k8s/registry/tests/test_registry_verify.py
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
        return lines + push(
            env["REGISTRY_URL"].rstrip("/"),
            env["REPO"],
            env["TAG"],
            push_retry_seconds,
        )
    if check == "isolation":
        urls = env.get("ISOLATED_URLS", "").split()
        if not urls:
            raise Failed("ISOLATED_URLS names no URL, so nothing would be checked")
        return lines + [isolated(url) for url in urls]
    raise Failed(f"CHECK is {check!r}; expected 'push' or 'isolation'")


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
