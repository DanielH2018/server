"""Declare Sonarr's air-date Release Profile, which rejects a release published before air.

Fakes race the real release: every fake episode this library was served in 2026 was
published days before its episode aired, and a real release almost never is. Sonarr v5's
Release Profile air-date restriction rejects a release whose indexer publish date is earlier
than each episode's air time plus the grace period, in hours. The grace is negative, so a
release published up to that many hours before air still passes.

The profile matches every series except those carrying an excluded tag. Anime is excluded,
because TVDB air dates for anime are off by hours to a week: at any grace up to 48 hours the
rule rejected at least 18 imported anime releases for 2 fakes in a backtest over this
library's history. An episode with no air date is rejected, which is Sonarr's own rule.

The Job writes only the profile it names and only the keys it declares, through the v5 API.
DECIDED: the v3 API is not used. Sonarr 5.0.0.3041's v3 resource maps `indexerId: 0` to
`IndexerIds = [0]`, which matches no real indexer, so a profile written through v3 never
applies.

Inputs come from the Job's environment: `SONARR_URL`, `SONARR_API_KEY` and
`SONARR_RELEASE_PROFILE`, a JSON object with `name`, `air_date_grace_hours` and
`excluded_tags`. `SONARR_DEPLOYMENT_DIGEST8` is in the spec only to rename the Job. Exit 0
when the profile matches the declaration, 1 when it cannot be made to, naming why.

Run: python3 seed_sonarr_release_profile.py (in the Job; it takes no arguments)
     uv run pytest k8s/roles/sonarr/tests/test_seed_sonarr_release_profile.py
"""

import argparse
import json
import os
import sys
import urllib.error
from collections.abc import Mapping
from dataclasses import dataclass

from flux_job_connect import Request, open_url

TIMEOUT = 30


class Failed(Exception):
    """The profile cannot be declared; the message names why."""


@dataclass(frozen=True)
class Declaration:
    """The profile this Job owns, as the role's defaults declare it."""

    name: str
    grace_hours: int
    excluded_tags: tuple[str, ...]

    @classmethod
    def parse(cls, text: str) -> "Declaration":
        raw = json.loads(text)
        return cls(
            name=str(raw["name"]),
            grace_hours=int(raw["air_date_grace_hours"]),
            excluded_tags=tuple(str(t) for t in raw["excluded_tags"]),
        )


def call(base_url: str, api_key: str, method: str, path: str, body=None):
    """One `/api/v5/<path>` call; the key travels as a header and is never printed."""
    data = json.dumps(body).encode() if body is not None else None
    request = Request(f"{base_url}/api/v5/{path}", data=data, method=method)
    request.add_header("X-Api-Key", api_key)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with open_url(request, timeout=TIMEOUT) as response:
            raw = response.read()
    except urllib.error.HTTPError as err:
        detail = err.read().decode(errors="replace")[:500]
        raise Failed(
            f"{method} /api/v5/{path} answered HTTP {err.code}: {detail}"
        ) from err
    except OSError as err:
        raise Failed(f"{method} /api/v5/{path} got no answer: {err}") from err
    return json.loads(raw) if raw.strip() else None


def tag_ids(tags: list) -> dict[str, int]:
    """Sonarr's `GET /tag` answer as a map from lowercased label to id."""
    return {str(t["label"]).lower(): int(t["id"]) for t in tags}


def desired_fields(declaration: Declaration, ids: dict[str, int]) -> dict:
    """The profile body this Job owns, with each excluded tag label resolved to its id.

    Raises:
        Failed: an excluded tag does not exist, so the series it should exempt are unknown.
    """
    missing = [label for label in declaration.excluded_tags if label.lower() not in ids]
    if missing:
        raise Failed(f"Sonarr has no tag {missing}, so the profile cannot exclude it")
    return {
        "name": declaration.name,
        "enabled": True,
        "required": [],
        "ignored": [],
        "airDateRestriction": True,
        "airDateGracePeriod": declaration.grace_hours,
        "allowSeasonPackWithoutAllEpisodesAired": False,
        "indexerIds": [],
        "tags": [],
        "excludedTags": sorted(
            ids[label.lower()] for label in declaration.excluded_tags
        ),
    }


def drift(live: dict, desired: dict) -> list[str]:
    """The owned keys whose live value differs from the declaration."""

    def norm(value):
        return sorted(value) if isinstance(value, list) else value

    return [key for key, value in desired.items() if norm(live.get(key)) != norm(value)]


def run(env: Mapping[str, str]) -> str:
    """Create or correct the profile, and return one line saying which.

    Raises:
        Failed: a call failed, or an excluded tag does not exist.
    """
    base_url, api_key = env["SONARR_URL"].rstrip("/"), env["SONARR_API_KEY"]
    declaration = Declaration.parse(env["SONARR_RELEASE_PROFILE"])
    desired = desired_fields(
        declaration, tag_ids(call(base_url, api_key, "GET", "tag"))
    )
    profiles = call(base_url, api_key, "GET", "releaseprofile")
    live = [p for p in profiles if p.get("name") == declaration.name]
    if len(live) > 1:
        raise Failed(f"{len(live)} release profiles are named {declaration.name!r}")
    if not live:
        created = call(base_url, api_key, "POST", "releaseprofile", desired)
        return f"created release profile {declaration.name!r} as id {created['id']}"
    changed = drift(live[0], desired)
    if not changed:
        return f"release profile {declaration.name!r} already matches; no change"
    call(
        base_url,
        api_key,
        "PUT",
        f"releaseprofile/{live[0]['id']}",
        {**live[0], **desired},
    )
    return f"updated release profile {declaration.name!r}: {', '.join(changed)}"


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    env = os.environ if env is None else env
    try:
        line = run(env)
    except Failed as err:
        print(f"sonarr-release-profile: {err}", file=sys.stderr)
        return 1
    print(
        f"sonarr-release-profile: Deployment {env.get('SONARR_DEPLOYMENT_DIGEST8', 'unknown')}: {line}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
