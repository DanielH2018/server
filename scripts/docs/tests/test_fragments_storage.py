"""Tests for the snapshot, Longhorn volume and deadman-grace fragments.

The renderers are pinned against literals the test supplies. The readers are pinned twice: a
fixture tree shows each one accepting and rejecting, and the real tree shows each still finds
the members it must. The include wiring is `test_gen_doc_fragments.py`'s job.
"""

import pytest

from fragments_lib import fragments_storage as s
from gen_doc_fragments import FRAGMENTS


def _role(roles_dir, name, defaults=None):
    role = roles_dir / name
    role.mkdir()
    if defaults is not None:
        (role / "defaults").mkdir()
        (role / "defaults" / "main.yml").write_text(defaults)


# --- snapshot-optin -------------------------------------------------------------------------


def test_a_role_declaring_the_key_is_counted(tmp_path):
    _role(tmp_path, "sonarr", "k8s_autodeploy_snapshot_pvcs: [sonarr-config]\n")
    assert s.snapshot_optin(tmp_path) == {"sonarr": ["sonarr-config"]}


def test_a_role_naming_the_key_only_in_a_comment_is_not_counted(tmp_path):
    _role(
        tmp_path, "observability", "# No `k8s_autodeploy_snapshot_pvcs` here.\nx: 1\n"
    )
    _role(tmp_path, "no-defaults")
    assert s.snapshot_optin(tmp_path) == {}


def test_the_real_tree_keeps_its_known_opt_ins_and_excludes_the_comment_only_roles():
    found = s.snapshot_optin()
    assert {"home-assistant", "code-server", "tdarr", "karakeep"} <= set(found)
    assert found["code-server"] == ["code-server-config", "code-server-workspace"]
    assert {"observability", "navidrome"}.isdisjoint(found)


def test_the_opt_in_table_counts_its_rows():
    body = s.render_snapshot_optin({"tdarr": ["tdarr-a", "tdarr-b"], "bazarr": ["b"]})
    assert body == (
        "2 k8s roles declare `k8s_autodeploy_snapshot_pvcs` in their defaults.\n"
        "\n"
        "| Role | Claims snapshotted before an apply |\n"
        "|---|---|\n"
        "| `bazarr` | `b` |\n"
        "| `tdarr` | `tdarr-a`, `tdarr-b` |\n"
    )


# --- longhorn-volume-shards -----------------------------------------------------------------


def test_the_volume_table_maps_each_tier_and_names_the_weekday():
    body = s.render_volume_shards(
        r2=["homelab/authelia-config"],
        weekly={"homelab/n8n-files": 5, "homelab/n8n-data": 0},
        nobackup=["homelab/crowdsec-db"],
        namespace="homelab",
    )
    assert body.splitlines()[2:] == [
        "| `authelia-config` | Daily | R2 | every day |",
        "| `n8n-data` | Weekly | B2 | Sunday |",
        "| `n8n-files` | Weekly | B2 | Friday |",
        "| `crowdsec-db` | None | — | — |",
    ]


def test_a_weekly_shard_outside_a_week_is_refused():
    with pytest.raises(ValueError, match="outside 0-6"):
        s.render_volume_shards([], {"homelab/x": 7}, [], "homelab")


def test_the_real_volume_fragment_carries_every_tier_with_its_known_members():
    body, _ = FRAGMENTS["longhorn-volume-shards"]()
    assert "| `authelia-config` | Daily | R2 | every day |" in body
    assert "| `n8n-data` | Weekly | B2 | Sunday |" in body
    assert "| `code-server-config` | None | — | — |" in body


# --- deadman-graces -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(1200, "20 minutes"), (1500, "25 minutes"), (3600, "1 hour"), (7200, "2 hours")],
)
def test_a_grace_reads_in_hours_when_whole_and_minutes_otherwise(seconds, text):
    assert s.format_grace(seconds) == text


def test_the_grace_table_has_a_row_per_slug():
    body = s.render_deadman_graces(
        [
            {"slug": "uptime-kuma-alive", "kind": "simple", "grace": 1200},
            {"slug": "registry-gc", "kind": "cron", "grace": 3600},
        ]
    )
    assert body.splitlines()[2:] == [
        "| `uptime-kuma-alive` | Simple | 20 minutes |",
        "| `registry-gc` | Cron | 1 hour |",
    ]


def test_the_real_grace_fragment_lists_the_wired_slugs():
    body, _ = FRAGMENTS["deadman-graces"]()
    assert "| `daniel-box-disk-health` | Simple | 25 minutes |" in body
    assert "| `pi-peer-backup` | Cron | 2 hours |" in body
    assert len(body.splitlines()) >= 2 + 11
