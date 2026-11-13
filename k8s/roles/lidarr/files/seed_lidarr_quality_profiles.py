"""Declare Lidarr's quality profiles instead of leaving them as UI state.

WHY THIS EXISTS. A quality profile is a row in `lidarr.db` on the `lidarr-config` claim, so a
Longhorn revert past its creation, or a recreated claim, drops it silently. The operator's
decision on #8190 fixed two profiles; this script makes the repo hold them.

IT ADOPTS BY NAME AND NEVER DELETES. Lidarr ships `Any`, `Lossless` and `Standard`. A declared
profile whose name matches a live one is updated in place, so the shipped `Lossless` and
`Standard` are adopted rather than duplicated. A name that matches nothing is created from
`GET /api/v1/qualityprofile/schema`. A live profile that no declaration names, `Any` included,
is left alone.

WHAT IT DECLARES. For each profile: `upgradeAllowed`, `cutoff`, and which qualities are
allowed, named by Lidarr's own quality names (`MP3-320`, `FLAC`). A name the app does not carry
fails the run, because a typo would otherwise leave that quality unallowed with no symptom.

WHY IT FLATTENS GROUPS. Lidarr's default profile groups qualities (`High Quality Lossy` holds
MP3-320, MP3-VBR-V0, AAC-VBR, AAC-320 and three Vorbis tiers) and `allowed` belongs to the
group, so "MP3-320 but not AAC-320" cannot be said while they share one. The declared body
holds every quality as its own item, in the order the app returned them, which is the order
that ranks them for upgrades. A group's id and name are dropped; no quality is.

WHAT IT LEAVES ALONE. `minFormatScore`, `cutoffFormatScore` and `formatItems` keep whatever
the returned body holds, because the update merges into that body rather than building one.

WHERE THE SECRET COMES FROM. `LIDARR_API_KEY` in the process environment, set by the seed Job
from the `lidarr-exportarr` Secret. Nothing here prints a field value: the output is one line
per profile naming the decision.

RUN IT: the seed Job invokes it with no arguments. Every request goes through
`flux_job_connect.open_url`, the helper the Job census requires of a script Flux runs. A hand
run sets `PYTHONPATH=scripts/renderer/job_files` for that helper. Nothing runs at import.
"""

import argparse
import json
import os
import sys
import urllib.error

from flux_job_connect import Request, open_url

API_PATH = "/api/v1"
HTTP_TIMEOUT = 30


class SeedError(Exception):
    """A condition the caller must fail on rather than report as configured."""


def deep_copy(body: dict) -> dict:
    """An independent copy of a JSON body, by a round trip; the Job census admits no `copy`."""
    return json.loads(json.dumps(body))


# ── the decision (pure, and the part the tests drive) ────────────────────────────────────────


def flat_items(items: list[dict]) -> list[dict]:
    """Every quality of a profile as its own item, in the order the app listed them.

    A group contributes its children where it stood, without its `id` or `name`.
    """
    flat = []
    for item in items:
        for leaf in item.get("items") or [item]:
            flat.append(deep_copy(leaf))
    return flat


def declared_body(
    existing: dict,
    *,
    name: str,
    allowed: list[str],
    cutoff: str,
    upgrade_allowed: bool,
) -> dict:
    """`existing` with everything this role declares applied, as a new dict.

    A deep copy merged into, never a body built from scratch: the API rejects a PUT missing a
    custom format, and a body assembled here would drop the scores the app holds.
    """
    items = flat_items(existing.get("items") or [])
    by_name = {item["quality"]["name"]: item for item in items}
    unknown = sorted(set(allowed) - by_name.keys())
    if unknown:
        raise SeedError(
            f"{name!r}: quality name(s) {unknown} are not ones the app carries — "
            f"known: {sorted(by_name)}"
        )
    if cutoff not in allowed:
        raise SeedError(
            f"{name!r}: the cutoff {cutoff!r} is not among its allowed qualities"
        )

    for item in items:
        item["allowed"] = item["quality"]["name"] in allowed
    body = deep_copy(existing)
    body["name"] = name
    body["items"] = items
    body["cutoff"] = by_name[cutoff]["quality"]["id"]
    body["upgradeAllowed"] = upgrade_allowed
    return body


def needs_update(existing: dict, desired: dict) -> bool:
    """True when the live profile is not already exactly what we declare.

    Compares parsed documents, not text: the API serialises its own key order.
    """
    return existing != desired


def find_profile(profiles: list[dict], name: str) -> dict | None:
    """The live profile carrying `name`, or None."""
    for entry in profiles:
        if entry.get("name") == name:
            return entry
    return None


# ── the transport ────────────────────────────────────────────────────────────────────────────


def request(base_url: str, api_key: str, path: str, method: str = "GET", body=None):
    """One Lidarr API call. The key travels as a header, never in the URL."""
    url = f"{base_url}{API_PATH}/{path.lstrip('/')}"
    data = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=data, method=method)
    req.add_header("X-Api-Key", api_key)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with open_url(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        # The app answers a rejected profile with a JSON body naming the field.
        raise SeedError(
            f"{method} {path} returned HTTP {exc.code}: {exc.read().decode()[:500]}"
        ) from exc
    except OSError as exc:
        raise SeedError(f"{method} {path} could not reach the app: {exc}") from exc
    return json.loads(raw) if raw.strip() else None


def env_or_fail(key: str) -> str:
    """A required environment value, absent or empty both failing the run."""
    if key not in os.environ:
        raise SeedError(f"{key} is not in this process's environment")
    value = os.environ[key].strip()
    if not value:
        raise SeedError(f"{key} is empty")
    return value


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    base_url = env_or_fail("LIDARR_URL").rstrip("/")
    api_key = env_or_fail("LIDARR_API_KEY")
    declared = json.loads(env_or_fail("LIDARR_QUALITY_PROFILES"))

    live = request(base_url, api_key, "qualityprofile")
    for spec in declared:
        existing = find_profile(live, spec["name"])
        base = existing or request(base_url, api_key, "qualityprofile/schema")
        desired = declared_body(
            base,
            name=spec["name"],
            allowed=spec["allowed"],
            cutoff=spec["cutoff"],
            upgrade_allowed=spec["upgrade_allowed"],
        )
        if existing is None:
            request(base_url, api_key, "qualityprofile", method="POST", body=desired)
            print(f"lidarr: quality profile {spec['name']!r} created")
        elif needs_update(existing, desired):
            request(
                base_url,
                api_key,
                f"qualityprofile/{existing['id']}",
                method="PUT",
                body=desired,
            )
            print(f"lidarr: quality profile {spec['name']!r} updated")
        else:
            print(
                f"lidarr: quality profile {spec['name']!r} already declares this spec — no change"
            )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SeedError as exc:
        sys.exit(f"seed_lidarr_quality_profiles: {exc}")
