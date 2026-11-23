"""AWS Signature Version 4 headers for an S3 request, which is how R2's S3 API authenticates.

The etcd drill's guest script signs its R2 requests with `curl --aws-sigv4 "aws:amz:auto:s3"`;
the pod's image ships no curl, so this is the same signature in stdlib Python. It signs `host`,
`x-amz-content-sha256` and `x-amz-date`, the three headers curl signs, and
`k8s/roles/monitor-bridge/tests/test_sigv4.py` holds it to signatures curl produced.
"""

import hashlib
import hmac
import time
import urllib.parse

_SAFE = "-_.~"


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sign(
    method: str,
    url: str,
    key_id: str,
    secret: str,
    body: bytes = b"",
    now: float | None = None,
    region: str = "auto",
) -> dict[str, str]:
    """The headers that authenticate `method url` with `body` to an S3 API.

    Args:
      method: The HTTP method the request is sent with.
      url: The full URL, with its path and query already percent-encoded as sent.
      key_id: The access key ID.
      secret: The secret access key.
      body: The request body, whose SHA-256 the signature covers.
      now: The signing time in epoch seconds; None is the current time.
      region: The signing region. R2 signs against `auto`.

    Returns:
      `Authorization`, `x-amz-content-sha256` and `x-amz-date`, to send beside the request's
      own headers. The `Host` header urllib sends must equal the URL's host.
    """
    parts = urllib.parse.urlsplit(url)
    stamp = time.strftime(
        "%Y%m%dT%H%M%SZ", time.gmtime(time.time() if now is None else now)
    )
    day = stamp[:8]
    payload_hash = hashlib.sha256(body).hexdigest()
    query = "&".join(
        "%s=%s" % (urllib.parse.quote(k, safe=_SAFE), urllib.parse.quote(v, safe=_SAFE))
        for k, v in sorted(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    )
    signed = "host;x-amz-content-sha256;x-amz-date"
    canonical = "\n".join(
        [
            method,
            parts.path or "/",
            query,
            "host:%s" % parts.netloc,
            "x-amz-content-sha256:%s" % payload_hash,
            "x-amz-date:%s" % stamp,
            "",
            signed,
            payload_hash,
        ]
    )
    scope = "%s/%s/s3/aws4_request" % (day, region)
    to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            stamp,
            scope,
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        ]
    )
    key = _hmac(("AWS4" + secret).encode("utf-8"), day)
    for part in (region, "s3", "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "Authorization": "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
        % (key_id, scope, signed, signature),
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": stamp,
    }
