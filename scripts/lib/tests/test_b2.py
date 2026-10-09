"""The shared B2 session: auth, both pagination shapes, and errors that end a listing."""

import base64
import io
from email.message import Message
import json
import urllib.error
import urllib.request

import pytest

from lib.b2 import AUTHORIZE_URL, B2Error, B2Session, http_json

AUTH = {
    "apiInfo": {
        "storageApi": {
            "apiUrl": "https://api.example/",
            "bucketId": "bid",
            "capabilities": ["listFiles", "deleteFiles"],
        }
    },
    "authorizationToken": "tok",
    "accountId": "acct",
}


class FakeTransport:
    """Answers each call with the next queued response and records what was asked."""

    def __init__(self, *responses: dict) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict, dict | None]] = []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append((url, headers, payload))
        return self.responses.pop(0)


def test_authorize_sends_the_key_as_basic_auth_and_reads_the_storage_api():
    transport = FakeTransport(AUTH)
    session = B2Session("kid", "secret", transport=transport)

    url, headers, payload = transport.calls[0]
    assert url == AUTHORIZE_URL and payload is None
    assert (
        headers["Authorization"] == "Basic " + base64.b64encode(b"kid:secret").decode()
    )
    assert session.api_url == "https://api.example"
    assert session.bucket_id == "bid" and "deleteFiles" in session.capabilities
    assert session.class_c == 1


def test_an_account_wide_key_has_no_bucket_until_one_is_looked_up():
    auth = {"apiUrl": "https://api", "authorizationToken": "t", "accountId": "acct"}
    transport = FakeTransport(auth, {"buckets": [{"bucketId": "found"}]})
    session = B2Session("k", "s", transport=transport)
    assert session.bucket_id is None
    with pytest.raises(B2Error, match="no bucket"):
        session.list_files("p/")

    assert session.lookup_bucket("backups") == "found"
    assert transport.calls[1][2] == {"accountId": "acct", "bucketName": "backups"}


def test_a_version_listing_pages_on_name_and_id():
    transport = FakeTransport(
        AUTH,
        {"files": [{"fileName": "a"}], "nextFileName": "b", "nextFileId": "id-b"},
        {"files": [{"fileName": "b"}]},
    )
    session = B2Session("k", "s", transport=transport)
    files = session.list_files("p/", versions=True)

    assert [f["fileName"] for f in files] == ["a", "b"]
    url, headers, payload = transport.calls[2]
    assert url.endswith("/b2api/v3/b2_list_file_versions")
    assert headers == {"Authorization": "tok"}
    assert payload is not None
    assert payload["startFileName"] == "b" and payload["startFileId"] == "id-b"
    assert session.list_calls == 2 and session.class_c == 3


def test_a_name_listing_ends_when_next_file_name_is_absent():
    """b2_list_file_names carries no file-id cursor, so a stray one must not keep it paging."""
    transport = FakeTransport(AUTH, {"files": [{"fileName": "a"}], "nextFileId": "x"})
    session = B2Session("k", "s", transport=transport)
    assert session.list_files("p/") == [{"fileName": "a"}]
    assert transport.calls[1][0].endswith("/b2_list_file_names")


def test_a_listing_past_its_page_cap_is_refused_rather_than_returned_partial():
    page = {"files": [{"fileName": "a"}], "nextFileName": "a", "nextFileId": "i"}
    session = B2Session("k", "s", transport=FakeTransport(AUTH, page, page))
    with pytest.raises(B2Error, match="did not finish within 2 pages"):
        session.list_files("p/", versions=True, max_pages=2)


def test_a_b2_error_status_raises_with_b2s_code(monkeypatch):
    """Under curl without --fail this body came back as an ordinary page with no files."""
    body = json.dumps(
        {"status": 403, "code": "transaction_cap_exceeded", "message": "cap"}
    ).encode()

    def refuse(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 403, "Forbidden", Message(), io.BytesIO(body)
        )

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(B2Error) as caught:
        http_json("https://api/b2api/v3/b2_list_file_names", {}, {"a": 1}, 5)
    assert caught.value.code == "transaction_cap_exceeded"
    assert "HTTP 403" in str(caught.value)


def test_a_2xx_json_body_is_parsed(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return b'{"files": []}'

    seen = {}

    def answer(request, timeout):
        seen["method"] = request.get_method()
        seen["type"] = request.get_header("Content-type")
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", answer)
    assert http_json("https://api/x", {}, {"a": 1}, 5) == {"files": []}
    assert seen == {"method": "POST", "type": "application/json"}
