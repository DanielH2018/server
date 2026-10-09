"""One Backblaze B2 native-API session: authorize, list a prefix, delete a version.

WHY. `scripts/backup/b2_drain.py` and `scripts/diagnostics/probe_lib/b2_api.py` each carried a
private B2 client: their own `b2_authorize_account`, their own pagination and their own
transport (`urllib` in one, `curl --config -` in the other). They disagreed on the one thing
that matters for a listing: whether a B2 error ends it. Under curl without `--fail`, a 4xx
arrived as an ordinary JSON body with no `files` and no `nextFileName`, so a listing refused
mid-way by `transaction_cap_exceeded` ended as if the prefix were complete. Here every non-2xx
raises `B2Error` carrying B2's own `code`.

TRANSPORT. `urllib`, in-process. The application key and the session token travel in request
headers inside this process, so no argv exists for `ps` to show — the property `curl --config -`
was chosen for, without the subprocess.

CREDENTIALS stay with the caller. The drain reads them from the environment because
`ansible/prune_backups.yml` decrypts them and hands them over; the probe decrypts them itself
with `sops`. This module never reads either source.
"""

import base64
import json
import urllib.error
import urllib.request
from collections.abc import Callable

AUTHORIZE_URL = "https://api.backblazeb2.com/b2api/v3/b2_authorize_account"
PAGE_SIZE = 1000

# (url, headers, JSON payload or None for a GET, timeout) -> the parsed JSON response.
Transport = Callable[[str, dict[str, str], dict | None, float], dict]


class B2Error(Exception):
    """A B2 call that failed: transport, a non-2xx status, or a body that is not JSON.

    `code` is B2's own error code (`transaction_cap_exceeded`, `unauthorized`, ...) when the
    response carried one, so a caller can tell a refused transaction cap from a missing bucket.
    """

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def http_json(
    url: str, headers: dict[str, str], payload: dict | None, timeout: float
) -> dict:
    """POST `payload` as JSON (or GET when it is None) and parse the response."""
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers = {**headers, "Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        # An HTTPError owns the open error response; left unclosed it raises a ResourceWarning
        # wherever garbage collection happens to run.
        with exc:
            raw = exc.read()
        try:
            err = json.loads(raw)
        except ValueError:
            err = {}
        code = err.get("code") if isinstance(err, dict) else None
        message = err.get("message") if isinstance(err, dict) else None
        raise B2Error(
            f"HTTP {exc.code} from {url.rsplit('/', 1)[-1]}: "
            f"{code or ''} {message or raw.decode(errors='replace')[:200]}".strip(),
            code=code,
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise B2Error(f"B2 request failed: {exc}") from None
    try:
        return json.loads(body)
    except ValueError:
        # A non-JSON 2xx is a proxy, DNS or truncation problem, so show the body verbatim.
        raise B2Error(
            "B2 returned a non-JSON body: " + body.decode(errors="replace")[:200]
        ) from None


class B2Session:
    """An authorized B2 session. Construction makes the `b2_authorize_account` call.

    `bucket_id` is set when the key is bucket-scoped and None otherwise. Only a caller that
    accepts an account-wide key resolves a bucket by name, through `lookup_bucket`; the drain
    refuses such a key rather than gaining reach over every bucket in the account.

    `class_c` counts every Class C transaction made (authorize, bucket lookup, list pages) and
    `list_calls` counts list pages alone.
    """

    def __init__(
        self,
        key_id: str,
        app_key: str,
        *,
        timeout: float = 120,
        transport: Transport = http_json,
    ) -> None:
        self._transport = transport
        self.timeout = timeout
        basic = base64.b64encode(f"{key_id}:{app_key}".encode()).decode()
        auth = transport(
            AUTHORIZE_URL, {"Authorization": f"Basic {basic}"}, None, timeout
        )
        storage = (auth.get("apiInfo") or {}).get("storageApi") or {}
        api_url = storage.get("apiUrl") or auth.get("apiUrl")
        token = auth.get("authorizationToken")
        if not api_url or not token:
            raise B2Error("B2 authorize returned no apiUrl/authorizationToken")
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.account_id = auth.get("accountId", "")
        self.bucket_id = storage.get("bucketId") or (auth.get("allowed") or {}).get(
            "bucketId"
        )
        self.capabilities = storage.get("capabilities") or []
        self.class_c = 1
        self.list_calls = 0

    def _call(self, api: str, payload: dict) -> dict:
        return self._transport(
            f"{self.api_url}/b2api/v3/{api}",
            {"Authorization": self.token},
            payload,
            self.timeout,
        )

    def lookup_bucket(self, name: str) -> str:
        """Resolve `name` to a bucket id and make it this session's bucket."""
        self.class_c += 1
        listed = self._call(
            "b2_list_buckets", {"accountId": self.account_id, "bucketName": name}
        )
        buckets = listed.get("buckets") or []
        if not buckets:
            raise B2Error(f"B2 has no bucket named {name}")
        self.bucket_id = buckets[0]["bucketId"]
        return self.bucket_id

    def list_files(
        self, prefix: str, *, versions: bool = False, max_pages: int | None = None
    ) -> list[dict]:
        """Every file under `prefix`, one Class C per 1,000 returned.

        `versions=True` lists every version (`b2_list_file_versions`, newest first per name,
        hide markers included); otherwise only current names (`b2_list_file_names`). A listing
        that has not finished after `max_pages` raises rather than returning a partial view.
        """
        if not self.bucket_id:
            raise B2Error(
                "no bucket: the key is not bucket-scoped and none was looked up"
            )
        api = "b2_list_file_versions" if versions else "b2_list_file_names"
        out: list[dict] = []
        start_name = start_id = None
        pages = 0
        while max_pages is None or pages < max_pages:
            payload: dict = {
                "bucketId": self.bucket_id,
                "prefix": prefix,
                "maxFileCount": PAGE_SIZE,
            }
            if start_name:
                payload["startFileName"] = start_name
            if start_id:
                payload["startFileId"] = start_id
            pages += 1
            self.list_calls += 1
            self.class_c += 1
            page = self._call(api, payload)
            out.extend(page.get("files") or [])
            start_name = page.get("nextFileName")
            start_id = page.get("nextFileId") if versions else None
            if not start_name and not start_id:
                return out
        raise B2Error(
            f"listing {prefix} did not finish within {max_pages} pages; refusing to act on a "
            "partial view of the prefix"
        )

    def delete(self, file_name: str, file_id: str) -> None:
        """Remove one file version. Class A, which B2 does not meter."""
        self._call("b2_delete_file_version", {"fileName": file_name, "fileId": file_id})
