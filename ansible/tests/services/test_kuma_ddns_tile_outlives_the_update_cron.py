"""The two Cloudflare DDNS push tiles wait longer than one DDNS cycle can take.

The DECIDED marker above `cloudflare-ddns-proxied-push.json` in `static-monitors.yaml.j2`
derives 360s: both DDNS pods run the image's default `UPDATE_CRON=@every 5m`, ping Kuma at
the END of a cycle, and the image caps a cycle's work at 5s of IP detection plus 30s of record
updating — so the latest beat lands at 335s, and a 300s tile flapped ~7 times a day. The
derivation has two inputs that live in two roles, and either can move alone: someone sets
`UPDATE_CRON` on a deployment to cure a monitoring artifact, or someone rounds the tile back
to 300. This reads both and re-derives the bound.

Run: uv run pytest ansible/tests/services/test_kuma_ddns_tile_outlives_the_update_cron.py
"""

import re

from _k8s_render import rendered_docs
from _kuma_entities import _entities

# favonia/cloudflare-ddns defaults, per the DECIDED marker: the cron period and the two
# per-cycle timeouts that bound how late a beat can land after the cron fires.
IMAGE_DEFAULT_PERIOD = 300
CYCLE_WORK_CAP = 5 + 30
TILES = {"Cloudflare DDNS Proxied", "Cloudflare DDNS Direct"}
EVERY = re.compile(r"@every (\d+)m$")


def cron_period(env: list[dict]) -> int:
    """The DDNS producer's period in seconds, from its `UPDATE_CRON` or the image default."""
    for var in env:
        if var.get("name") == "UPDATE_CRON":
            match = EVERY.match(str(var.get("value", "")))
            assert match, (
                f"UPDATE_CRON {var.get('value')!r} is not the `@every Nm` form"
            )
            return int(match.group(1)) * 60
    return IMAGE_DEFAULT_PERIOD


def _ddns_envs() -> dict[str, list[dict]]:
    envs = {}
    for role, _tpl, doc in rendered_docs():
        if role == "cloudflare-ddns" and doc.get("kind") == "Deployment":
            (container,) = doc["spec"]["template"]["spec"]["containers"]
            envs[doc["metadata"]["name"]] = container.get("env", [])
    assert len(envs) == 2, sorted(envs)
    # The selector reaches the env list it would find UPDATE_CRON in: both carry DOMAINS.
    assert all("DOMAINS" in {v.get("name") for v in env} for env in envs.values()), envs
    return envs


def tile_outlives_cycle(interval: int, envs: dict[str, list[dict]]) -> bool:
    """True when a beat from the slowest producer still lands inside the tile's deadline."""
    return interval > max(cron_period(env) + CYCLE_WORK_CAP for env in envs.values())


def test_each_ddns_tile_interval_exceeds_the_slowest_possible_cycle():
    tiles = {
        e["name"]: e["interval"] for e in _entities().values() if e.get("name") in TILES
    }
    assert set(tiles) == TILES
    envs = _ddns_envs()
    for name, interval in tiles.items():
        assert tile_outlives_cycle(interval, envs), (
            f"{name} waits {interval}s, less than one DDNS cycle can take; re-derive the "
            "tile from the DECIDED marker in static-monitors.yaml.j2"
        )


def test_a_slower_cron_or_a_rounded_tile_would_be_caught():
    default = {"proxied": [], "direct": []}
    slower = {"proxied": [{"name": "UPDATE_CRON", "value": "@every 10m"}], "direct": []}
    assert cron_period(slower["proxied"]) == 600
    assert tile_outlives_cycle(360, default)
    assert not tile_outlives_cycle(300, default)
    assert not tile_outlives_cycle(360, slower)
