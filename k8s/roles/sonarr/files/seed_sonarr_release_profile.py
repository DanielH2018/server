"""Declare Sonarr's air-date Release Profile, which rejects a release published before air.

Fakes race the real release: every fake episode this library was served in 2026 was
published days before its episode aired, and a real release almost never is. Sonarr v5's
Release Profile air-date restriction rejects a release whose indexer publish date is earlier
than each episode's air time plus the grace period, in hours. The grace is negative, so a
release published up to that many hours before air still passes.

The profile matches every series except those carrying an excluded tag. A Release Profile
can exclude only by tag, so each excluded series type gets an Auto Tagging rule that adds a
tag named after the type to every series of that type, and that tag is excluded too. Sonarr
applies the rule when it adds or refreshes a series, so the Job starts one refresh of every
series after it creates or corrects a rule. Anime is excluded, because TVDB air dates for
anime are off by hours to a week: at any grace up to 48 hours the rule rejected at least 18
imported anime releases for 2 fakes in a backtest over this library's history. An episode
with no air date is rejected, which is Sonarr's own rule.

The Job writes only the profile and rules it names and only the keys it declares, through
the v5 API. A rule never removes its tag, so a tag added by hand stays.
DECIDED: the v3 API is not used. Sonarr 5.0.0.3041's v3 resource maps `indexerId: 0` to
`IndexerIds = [0]`, which matches no real indexer, so a profile written through v3 never
applies.

Inputs come from the Job's environment: `SONARR_URL`, `SONARR_API_KEY` and
`SONARR_RELEASE_PROFILE`, a JSON object with `name`, `air_date_grace_hours`,
`excluded_tags` and `excluded_series_types`. `SONARR_DEPLOYMENT_DIGEST8` is in the spec only
to rename the Job. Exit 0 when Sonarr matches the declaration, 1 when it cannot be made to,
naming why.

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
# Sonarr's SeriesTypes enum.
SERIES_TYPES = {"standard": 0, "daily": 1, "anime": 2}


class Failed(Exception):
    """Sonarr cannot be made to match the declaration; the message names why."""


@dataclass(frozen=True)
class Declaration:
    """The profile and rules this Job owns, as the role's defaults declare them."""

    name: str
    grace_hours: int
    excluded_tags: tuple[str, ...]
    excluded_series_types: tuple[str, ...]

    @classmethod
    def parse(cls, text: str) -> "Declaration":
        """Read the declaration's JSON.

        Raises:
            Failed: a series type is not one Sonarr has.
        """
        raw = json.loads(text)
        series_types = tuple(str(t).lower() for t in raw["excluded_series_types"])
        unknown = [t for t in series_types if t not in SERIES_TYPES]
        if unknown:
            raise Failed(f"Sonarr has no series type {unknown}")
        return cls(
            name=str(raw["name"]),
            grace_hours=int(raw["air_date_grace_hours"]),
            excluded_tags=tuple(str(t) for t in raw["excluded_tags"]),
            excluded_series_types=series_types,
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
    labels = declaration.excluded_tags + declaration.excluded_series_types
    missing = [label for label in labels if label.lower() not in ids]
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
        "excludedTags": sorted({ids[label.lower()] for label in labels}),
    }


def desired_auto_tag(series_type: str, tag_id: int) -> dict:
    """The Auto Tagging rule that adds `tag_id` to every series of `series_type`."""
    return {
        "name": f"Tag {series_type} series",
        "removeTagsAutomatically": False,
        "tags": [tag_id],
        "specifications": [
            {
                "name": "Series type",
                "implementation": "SeriesTypeSpecification",
                "negate": False,
                "required": True,
                "fields": [{"name": "value", "value": SERIES_TYPES[series_type]}],
            }
        ],
    }


def _specification_key(spec: dict) -> tuple:
    values = {f["name"]: f.get("value") for f in spec.get("fields", [])}
    return (
        spec.get("implementation"),
        bool(spec.get("negate")),
        bool(spec.get("required")),
        tuple(sorted(values.items())),
    )


def drift(live: dict, desired: dict) -> list[str]:
    """The owned keys whose live value differs from the declaration."""

    def norm(key, value):
        if key == "specifications":
            return sorted(_specification_key(s) for s in value or [])
        return sorted(value) if isinstance(value, list) else value

    return [
        key
        for key, value in desired.items()
        if norm(key, live.get(key)) != norm(key, value)
    ]


def declare(
    base_url: str, api_key: str, resource: str, label: str, desired: dict
) -> str | None:
    """Create or correct the `resource` named `desired["name"]`.

    Returns:
        One line saying what was written, or None when it already matched.

    Raises:
        Failed: a call failed, or more than one object carries the name.
    """
    name = desired["name"]
    live = [
        item
        for item in call(base_url, api_key, "GET", resource)
        if item.get("name") == name
    ]
    if len(live) > 1:
        raise Failed(f"{len(live)} {label}s are named {name!r}")
    if not live:
        created = call(base_url, api_key, "POST", resource, desired)
        return f"created {label} {name!r} as id {created['id']}"
    changed = drift(live[0], desired)
    if not changed:
        return None
    call(
        base_url, api_key, "PUT", f"{resource}/{live[0]['id']}", {**live[0], **desired}
    )
    return f"updated {label} {name!r}: {', '.join(changed)}"


def run(env: Mapping[str, str]) -> str:
    """Bring Sonarr to the declaration, and return one line saying what changed.

    Raises:
        Failed: a call failed, a series type is unknown, or an excluded tag does not exist.
    """
    base_url, api_key = env["SONARR_URL"].rstrip("/"), env["SONARR_API_KEY"]
    declaration = Declaration.parse(env["SONARR_RELEASE_PROFILE"])
    ids = tag_ids(call(base_url, api_key, "GET", "tag"))
    lines = []
    for series_type in declaration.excluded_series_types:
        if series_type not in ids:
            created = call(base_url, api_key, "POST", "tag", {"label": series_type})
            ids[series_type] = int(created["id"])
            lines.append(f"created tag {series_type!r} as id {created['id']}")
    rules_changed = False
    for series_type in declaration.excluded_series_types:
        line = declare(
            base_url,
            api_key,
            "autotagging",
            "auto tagging rule",
            desired_auto_tag(series_type, ids[series_type]),
        )
        if line:
            lines.append(line)
            rules_changed = True
    if rules_changed:
        call(base_url, api_key, "POST", "command", {"name": "RefreshSeries"})
        lines.append("started a refresh of every series to apply the rules")
    line = declare(
        base_url,
        api_key,
        "releaseprofile",
        "release profile",
        desired_fields(declaration, ids),
    )
    if line:
        lines.append(line)
    return "; ".join(lines) or "everything already matches; no change"


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
