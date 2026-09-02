"""gen_doc_fragments:

the readers find their sources, the renderers are pure, and every fragment is wired both ways -- a
page includes it, and it exists for the page to include.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gen_doc_fragments as g

DOCS = g.REPO / "docs"
INCLUDE = re.compile(
    r'^--8<-- "assets/generated/fragments/([\w-]+)\.md"$', re.MULTILINE
)


def _includes() -> dict[str, list[Path]]:
    """fragment name -> the pages that include it. Walks every page mkdocs would build."""
    found: dict[str, list[Path]] = {}
    for page in DOCS.rglob("*.md"):
        if "assets/generated" in page.as_posix():
            continue
        for name in INCLUDE.findall(page.read_text()):
            found.setdefault(name, []).append(page)
    return found


# --- wiring: both directions ---------------------------------------------------------------


def test_every_include_names_a_fragment_this_script_emits():
    unknown = {n: p for n, p in _includes().items() if n not in g.FRAGMENTS}
    assert not unknown, f"includes with no generator: {unknown}"


def test_every_fragment_is_included_by_at_least_one_page():
    dead = sorted(set(g.FRAGMENTS) - set(_includes()))
    assert not dead, (
        f"generated but never included, so nothing would notice it rotting: {dead}"
    )


def test_the_include_scan_finds_the_known_corpus():
    # A regex that stops matching would make both checks above pass on nothing.
    assert len(_includes()) >= 7


def test_every_committed_fragment_matches_what_the_generator_writes_now(tmp_path):
    """The cron regenerates these; a committed fragment that differs is a hand edit."""
    g.write_fragments(tmp_path)
    committed = g.REPO / g.DEFAULT_OUT_DIR
    for name in g.FRAGMENTS:
        assert (tmp_path / f"{name}.md").read_text() == (
            committed / f"{name}.md"
        ).read_text(), f"{name}.md is stale or hand-edited: run {g.SELF}"


# --- the header is what the edit hook keys on ------------------------------------------------


def test_the_header_carries_the_provenance_marker_the_hook_reads():
    # block-protected-edits.py exempts a .md under docs/assets/generated that LACKS
    # `generated_from:`, so a fragment without it would be silently hand-editable.
    assert "generated_from:" in g.header(["x"])
    assert g.header(["x"]).startswith("<!--")


def test_the_header_is_one_line_with_no_frontmatter():
    # A snippet is spliced in verbatim; a `---` block would render as a rule plus text.
    line = g.header(["a", "b"])
    assert line.count("\n") == 1
    assert "---" not in line


# --- readers against the real tree ---------------------------------------------------------


def test_module_constant_reads_a_tuple_without_importing():
    prefixes = g.module_constant(g.DEPLOY_CHANGES, "_BROAD_MANUAL_PREFIXES")
    assert "ansible/bootstrap.yml" in prefixes


def test_module_constant_rejects_a_name_that_is_not_assigned():
    with pytest.raises(KeyError):
        g.module_constant(g.DEPLOY_CHANGES, "_NO_SUCH_CONSTANT")


def test_config_default_reads_the_fallback_string():
    assert "traefik" in g.config_default(g.GITOPS_DEPLOY, "STAGING_SUBSET")


def test_config_default_rejects_a_key_nothing_reads():
    with pytest.raises(KeyError):
        g.config_default(g.GITOPS_DEPLOY, "NO_SUCH_KEY")


def test_registry_counts_cover_every_registered_secret():
    counts = g.registry_counts(g.SECRET_REGISTRY)
    assert sum(counts.values()) > 100
    assert "auto" in counts


# --- renderers: pure, and they can go red ----------------------------------------------------

_DEFAULTS = {
    "k3s_longhorn_r2_volumes": ["homelab/a", "homelab/b"],
    "k3s_longhorn_weekly_volumes": ["homelab/c"],
    "k3s_longhorn_nobackup_volumes": [],
    "k3s_longhorn_weekly_backup_minute_hour": "30 4",
    "k3s_longhorn_backup_armed": False,
    "k3s_longhorn_backup_cron": "30 3 * * *",
    "k3s_longhorn_backup_retain": 14,
    "k3s_longhorn_weekly_backup_retain": 2,
    "k3s_longhorn_daily_backup_budget": 16,
}


def test_longhorn_tiers_renders_the_defaults_it_is_given():
    out = g.render_longhorn_tiers(_DEFAULTS)
    assert "| Daily | R2 (`r2`) | 2 | `30 3 * * *` | 14 |" in out
    assert "| 1, one weekday each | `30 4 * * <index mod 7>` | 2 |" in out
    assert "`homelab/a`, `homelab/b`" in out


def test_longhorn_tiers_says_when_backups_are_disarmed():
    assert "**disarmed**" in g.render_longhorn_tiers(_DEFAULTS)
    assert "are armed" in g.render_longhorn_tiers(
        {**_DEFAULTS, "k3s_longhorn_backup_armed": True}
    )


def test_longhorn_tiers_fails_on_a_missing_default_rather_than_guessing():
    with pytest.raises(KeyError):
        g.render_longhorn_tiers(
            {k: v for k, v in _DEFAULTS.items() if "retain" not in k}
        )


def test_gitops_prefixes_lists_every_prefix_under_its_constant():
    out = g.render_gitops_prefixes(("s/",), ("d/", "e/"), ("m.yml",))
    assert "| Setup, scoped | `s/` (`_BROAD_SETUP_PREFIXES`)" in out
    assert "`d/`, `e/` (`_BROAD_DEPLOY_PREFIXES`)" in out
    assert "`m.yml` (`_BROAD_MANUAL_PREFIXES`)" in out


def test_staging_subset_sorts_and_counts():
    out = g.render_staging_subset("traefik, authelia,freshrss")
    assert "`authelia`, `freshrss`, `traefik` — 3 services" in out


def test_secret_tiers_renders_cadence_and_count_per_tier():
    out = g.render_secret_tiers({"auto": 180, "ignore": None}, 8, {"auto": 3})
    assert "| `auto` | 180 d | 3 |" in out
    assert "| `ignore` | — | 0 |" in out
    assert "3 secrets are registered" in out
    assert "`ROTATE_LEAD_DAYS` = 8" in out


# --- write policy ----------------------------------------------------------------------------


def test_a_second_run_writes_nothing(tmp_path):
    assert g.write_fragments(tmp_path) == len(g.FRAGMENTS)
    assert g.write_fragments(tmp_path) == 0


# --- fail2ban, deadman cadences, LAN addresses ---------------------------------------------

_CONF = """\
[DEFAULT]
bantime = 1h
findtime = 10m
maxretry = 5

[sshd]
enabled = true

[dovecot]
enabled = false

[recidive]
enabled = true
bantime = 7d
findtime = 1d
maxretry = 3
"""


def test_parse_jails_resolves_defaults_and_skips_disabled():
    jails = g.parse_jails(_CONF)
    assert [j["jail"] for j in jails] == ["sshd", "recidive"]
    assert jails[0] == {
        "jail": "sshd",
        "maxretry": "5",
        "findtime": "10m",
        "bantime": "1h",
    }
    assert jails[1]["bantime"] == "7d"


def test_fail2ban_table_renders_one_row_per_enabled_jail():
    out = g.render_fail2ban_jails(g.parse_jails(_CONF))
    assert "| `sshd` | 5 failures in 10m (`maxretry 5`, `findtime 10m`) | 1h |" in out
    assert "dovecot" not in out


def test_container_udp_port_reads_the_named_entry():
    hv = {
        "containers_list": [
            {"name": "a", "udp_port": 1},
            {"name": "wg-easy", "udp_port": 51820},
        ]
    }
    assert g.container_udp_port(hv, "wg-easy") == "51820"


def test_container_udp_port_refuses_an_ambiguous_or_missing_entry():
    hv = {
        "containers_list": [
            {"name": "wg-easy", "udp_port": 1},
            {"name": "wg-easy", "udp_port": 2},
        ]
    }
    with pytest.raises(AssertionError):
        g.container_udp_port(hv, "wg-easy")
    with pytest.raises(AssertionError):
        g.container_udp_port({"containers_list": []}, "wg-easy")


def test_deadman_cadences_assembles_each_cron_from_its_variables():
    k3s = {
        "k3s_longhorn_backup_health_cron_minute": "*/10",
        "k3s_disk_health_cron_minute": "*/5",
        "k3s_etcd_s3_cron_hour": "2",
        "k3s_etcd_s3_cron_minute": "45",
        "k3s_manifest_prune_cron_hour": "5",
        "k3s_manifest_prune_cron_minute": "15",
    }
    registry = {
        "registry_k8s_gc_cron_weekday": "0",
        "registry_k8s_gc_cron_hour": "4",
        "registry_k8s_gc_cron_minute": "20",
    }
    out = g.render_deadman_cadences(
        k3s, {"pi_peer_backup_k8s_schedule": "30 23 * * *"}, registry
    )
    assert "| `daniel-box-disk-health` | `*/5 * * * *` |" in out
    assert "| `etcd-snapshot-offbox` | `45 2 * * *` |" in out
    assert "| `registry-gc` | `20 4 * * 0` |" in out
    assert "| `uptime-kuma-alive` | `*/10 * * * *` |" in out


def test_deadman_cadences_fails_on_a_missing_variable_rather_than_guessing():
    with pytest.raises(KeyError):
        g.render_deadman_cadences({}, {"pi_peer_backup_k8s_schedule": "x"}, {})


def test_lan_addresses_names_each_value_and_its_variable():
    out = g.render_lan_addresses("10.0.0.240", "10.0.0.243", "51820", "51822")
    assert "| `10.0.0.240` | `k3s_metallb_ingress_vip` |" in out
    assert "| `51822/udp` |" in out
