"""The weekly and no-backup Longhorn tiers derive from containers_list entries (#4207).

`k3s_longhorn_weekly_volumes` and `k3s_longhorn_nobackup_volumes` were hand lists in the k3s
role's defaults. A weekly volume's weekday was its index mod 7 in its list. Each entry now
declares `weekly_backup_claims` (claim to shard) and `no_backup_claims`, read by
`filter_plugins/longhorn_groups.py`. These tests pin the derivation to the lists it replaced,
so the migration moved no volume to another weekday. A shard that later moves on purpose
edits the table below in the same change.

Run: uv run pytest ansible/tests/longhorn/test_weekly_and_nobackup_tiers_are_derived.py
"""

import pytest

from _helpers import load_defaults
from lib.repo_paths import K3S_ROLE
from lib.service_tiers import resolved_tier_lists

# Each volume of the replaced weekly list, with its index mod 7 in that list.
_REPLACED_WEEKLY = {
    "homelab/code-server-workspace": 0,
    "homelab/jellyfin-config": 1,
    "homelab/valheim-config": 2,
    "homelab/karakeep-data": 3,
    "homelab/prowlarr-config": 4,
    "homelab/freshrss-config": 5,
    "homelab/sonarr-config": 6,
    "homelab/n8n-data": 0,
    "homelab/tdarr-server": 1,
    "homelab/radarr-config": 2,
    "homelab/terraria-config": 3,
    "homelab/qbittorrent-config": 4,
    "homelab/bazarr-config": 5,
    "homelab/scrutiny-web-config": 6,
    "homelab/terraria-stats-data": 0,
    "homelab/valheim-stats-data": 1,
    "homelab/tdarr-configs": 2,
    "homelab/navidrome-data": 3,
    "homelab/wg-easy-config": 4,
    "homelab/n8n-files": 5,
    "homelab/pi-peer-backup-data": 6,
}

_REPLACED_NOBACKUP = frozenset(
    {
        "homelab/autokuma-data",
        "homelab/scrutiny-influxdb-data",
        "homelab/uptime-kuma-data",
        "homelab/crowdsec-db",
        "homelab/code-server-config",
    }
)


@pytest.mark.parametrize(
    ("key", "filter_name"),
    [
        ("k3s_longhorn_weekly_volumes", "longhorn_weekly_claims"),
        ("k3s_longhorn_nobackup_volumes", "longhorn_nobackup_claims"),
    ],
)
def test_the_role_default_is_the_derivation_not_a_list(key, filter_name):
    raw = load_defaults(K3S_ROLE)[key]
    assert isinstance(raw, str) and filter_name in raw, raw


def test_no_weekly_volume_changed_its_weekday():
    derived = resolved_tier_lists(load_defaults(K3S_ROLE))[
        "k3s_longhorn_weekly_volumes"
    ]
    assert derived == _REPLACED_WEEKLY


def test_the_nobackup_set_is_the_list_it_replaced():
    derived = resolved_tier_lists(load_defaults(K3S_ROLE))[
        "k3s_longhorn_nobackup_volumes"
    ]
    assert len(derived) == len(set(derived)), derived
    assert set(derived) == _REPLACED_NOBACKUP
