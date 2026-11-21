"""Write the SOPS Sonarr, Radarr and Lidarr API keys into Prowlarr's application entries.

WHY THIS EXISTS. Prowlarr keeps its own copy of each app's API key in `prowlarr.db`
(Settings -> Apps), on the `prowlarr-config` claim. A key rotation in SOPS, or a revert of that
claim, leaves the copy stale, and every indexer push to that app then fails with a 401.

HOW IT DETECTS DRIFT. `GET /api/v1/applications` masks `apiKey` as `********`, so the stored
key cannot be compared. Instead the script sends each entry, unchanged, to
`POST /api/v1/applications/test`. Prowlarr swaps its stored key back in for the mask and tests
the app with it, so a 2xx means the stored key works and the entry is left alone. A PUT is not
free: Prowlarr tests the app again and pushes every indexer to it, so the script writes only
when the test names `ApiKey` among its failures. Any other failure fails the Job, because a PUT
would hit the same failure.

WHAT IT WRITES. The `apiKey` field alone, merged into the body the GET returned. The other
private fields come back masked too, and Prowlarr keeps their stored values on a PUT.

WHAT IT REFUSES. An app with no entry, or with two, fails the Job. It never creates or deletes
an application: an entry needs Prowlarr's and the app's URLs and a sync level, which are UI
state this script does not own.

WHERE THE SECRETS COME FROM. The environment: `PROWLARR_API_KEY` and one `<APP>_API_KEY` per
app, which the Job fills from the apps' `<app>-exportarr` Secrets. Nothing here prints a key:
the output is one count line.

Every request goes through `flux_job_connect.open_url`, the helper the Job census requires of
a script Flux runs, and the script imports only modules that census admits.
"""

import argparse
import json
import os
import sys
import urllib.error
from dataclasses import dataclass, replace

from flux_job_connect import Request, open_url

# Each app's `implementation` in Prowlarr's application list.
APPS = ("Sonarr", "Radarr", "Lidarr")
# The `name` of the key's entry in an application's `fields`.
KEY_FIELD = "apiKey"
HTTP_TIMEOUT = 30


class SeedError(Exception):
    """A condition the Job must fail on rather than report as seeded."""


class ApiError(SeedError):
    """Prowlarr answered with an HTTP error.

    Attributes:
        code: The HTTP status.
        body: The response body, decoded.
    """

    code = 0
    body = ""


def request(api_url: str, api_key: str, path: str, method: str = "GET", body=None):
    """One Prowlarr API call under `api_url`, returning the parsed JSON or None.

    Raises:
        ApiError: Prowlarr answered with an HTTP error.
        SeedError: Prowlarr could not be reached.
    """
    data = json.dumps(body).encode() if body is not None else None
    req = Request(f"{api_url}/{path}", data=data, method=method)
    req.add_header("X-Api-Key", api_key)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with open_url(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        error = ApiError(f"{method} {path} returned HTTP {exc.code}: {body[:500]}")
        error.code = exc.code
        error.body = body
        raise error from exc
    except OSError as exc:
        raise SeedError(f"{method} {path} could not reach Prowlarr: {exc}") from exc
    return json.loads(raw) if raw.strip() else None


@dataclass(frozen=True)
class Application:
    """One entry of Prowlarr's application list.

    Attributes:
        id: The entry's id, which its PUT path names.
        implementation: The app it points at, such as `Sonarr`.
        body: The entry as the API returned it, which a test or a PUT sends back.
    """

    id: int
    implementation: str
    body: dict


@dataclass(frozen=True)
class Failure:
    """One validation failure from Prowlarr's test of an application.

    Attributes:
        field: The setting it names, such as `ApiKey`.
        message: Prowlarr's message for it.
    """

    field: str
    message: str


def parse_applications(rows: list) -> list[Application]:
    """The entries of `GET /api/v1/applications`."""
    return [Application(int(r["id"]), str(r["implementation"]), r) for r in rows]


def find_application(
    applications: list[Application], implementation: str
) -> Application:
    """The one application entry of `implementation`.

    Raises:
        SeedError: no entry, or more than one, carries it.
    """
    found = [a for a in applications if a.implementation == implementation]
    if len(found) != 1:
        raise SeedError(
            f"Prowlarr has {len(found)} {implementation} applications, not one"
        )
    return found[0]


def key_is_rejected(failures: list[Failure]) -> bool:
    """Whether a failed application test names the API key.

    Prowlarr answers a test that the app refused with a 401 as the failure
    `{"propertyName": "ApiKey", "errorMessage": "API Key is invalid"}`.
    """
    return any(f.field.lower() == KEY_FIELD.lower() for f in failures)


def with_key(application: Application, api_key: str) -> Application:
    """A copy of `application` whose `apiKey` field holds `api_key`.

    Raises:
        SeedError: the entry has no `apiKey` field.
    """
    body = json.loads(json.dumps(application.body))
    for field in body.get("fields") or []:
        if field.get("name") == KEY_FIELD:
            field["value"] = api_key
            return replace(application, body=body)
    raise SeedError(
        f"the {application.implementation} application has no {KEY_FIELD!r} field"
    )


def parse_failures(body: str) -> list[Failure] | None:
    """The validation failures a 400 body lists, or None when it is not such a list."""
    try:
        rows = json.loads(body)
    except ValueError:
        return None
    if not isinstance(rows, list):
        return None
    return [
        Failure(str(r.get("propertyName", "")), str(r.get("errorMessage", "")))
        for r in rows
        if isinstance(r, dict)
    ]


def stored_key_failures(
    api_url: str, api_key: str, application: Application
) -> list[Failure]:
    """The failures of Prowlarr's test of `application` with its stored key; empty on a pass.

    Raises:
        ApiError: the test answered anything but 2xx or a 400 failure list.
    """
    try:
        request(
            api_url, api_key, "applications/test", method="POST", body=application.body
        )
    except ApiError as exc:
        failures = parse_failures(exc.body) if exc.code == 400 else None
        if failures is None:
            raise
        return failures
    return []


def seed(api_url: str, api_key: str, app_keys: dict[str, str]) -> list[str]:
    """Write each stale key in `app_keys` into its Prowlarr entry, returning the apps written.

    Args:
        api_url: Prowlarr's `/api/v1` URL.
        api_key: Prowlarr's own API key.
        app_keys: Each app's `implementation` mapped to the key the app accepts.

    Raises:
        SeedError: an app has no single entry, or its test fails on anything but the key.
    """
    applications = parse_applications(request(api_url, api_key, "applications"))
    written = []
    for implementation, app_key in app_keys.items():
        application = find_application(applications, implementation)
        failures = stored_key_failures(api_url, api_key, application)
        if not failures:
            continue
        if not key_is_rejected(failures):
            reasons = sorted({f.message for f in failures})
            raise SeedError(
                f"Prowlarr's test of its {implementation} application failed: {reasons}"
            )
        request(
            api_url,
            api_key,
            f"applications/{application.id}",
            method="PUT",
            body=with_key(application, app_key).body,
        )
        written.append(implementation)
    return written


def env_or_fail(key: str) -> str:
    """A required, non-empty environment value."""
    value = os.environ.get(key, "").strip()
    if not value:
        raise SeedError(f"{key} is missing or empty in this process's environment")
    return value


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    written = seed(
        f"{env_or_fail('PROWLARR_URL')}/api/v1",
        env_or_fail("PROWLARR_API_KEY"),
        {app: env_or_fail(f"{app.upper()}_API_KEY") for app in APPS},
    )
    listed = f" ({', '.join(written)})" if written else ""
    print(
        f"prowlarr applications: {len(written)} keys written{listed}, "
        f"{len(APPS) - len(written)} unchanged"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SeedError as exc:
        sys.exit(f"seed_prowlarr_app_keys: {exc}")
