"""The signer against signatures `curl --aws-sigv4 "aws:amz:auto:s3"` produced.

curl 8.18.0 signed each request at 2026-11-23T15:17:37Z with the key below, and a local listener
recorded its `Authorization` header. curl is the signer the etcd drill's guest script uses
against the same bucket.
"""

import calendar
import time

import pytest

from bridge.sigv4 import sign

KEY_ID = "AKIDEXAMPLE"
SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
SIGNED_AT = calendar.timegm(time.strptime("20261123T151737Z", "%Y%m%dT%H%M%SZ"))
BUCKET = "https://acct.r2.cloudflarestorage.com/homelab-r2"


@pytest.mark.parametrize(
    "method, url, signature",
    [
        (
            "GET",
            BUCKET + "/?list-type=2&prefix=etcd-snapshots%2Flock-canary-",
            "8eda8e3d52e8ab72309080ae37d207ccea97845cc16a61a3830cf88c13ba2170",
        ),
        (
            "PUT",
            BUCKET + "/etcd-snapshots/lock-canary-2026-11-23",
            "8b381b36bd610096169254caa794bb7a3e046475eff0396022cdb89fa1a36a18",
        ),
        (
            "DELETE",
            BUCKET + "/etcd-snapshots/lock-canary-2026-11-22",
            "08b345c037c04590b938d5987ea3fba1aa9588179de61088f76a0d985bce76f8",
        ),
    ],
    ids=["list", "put", "delete"],
)
def test_the_signature_matches_curls(method, url, signature):
    headers = sign(method, url, KEY_ID, SECRET, now=SIGNED_AT)
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20261123/auto/s3/aws4_request, "
        "SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature=%s" % signature
    )
    assert headers["x-amz-date"] == "20261123T151737Z"
