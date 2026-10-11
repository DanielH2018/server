"""Tests for the host, network and per-service pin fragments in `fragments_lib/fragments_hosts.py`.

Each renderer is pinned against literals it is handed. Each reader that finds its subject by
pattern is run twice: against crafted text it must accept or reject, and against the real
tree, where it must name a member it is known to find.
"""

from fragments_lib import fragments_hosts as h
from lib.estate import Estate

# --- renderers, against literals -------------------------------------------------------------


def test_cni0_gateways_render_one_bullet_per_cidr():
    assert h.render_cni0_gateways(["10.1.0.1/32", "10.1.1.1/32"]) == (
        "- `10.1.0.1/32`\n- `10.1.1.1/32`\n"
    )


def test_loadbalancers_render_one_row_per_service():
    body = h.render_loadbalancers(
        [
            {"role": "a", "service": "a-lan", "etp": "Local"},
            {"role": "b", "service": "b", "etp": "Cluster"},
        ]
    )
    assert "| `a` | `a-lan` | `Local` |" in body
    assert "| `b` | `b` | `Cluster` |" in body


def test_audit_watches_group_paths_under_their_key():
    body = h.render_audit_watches(
        [("/a", "wa", "k1"), ("/b", "wa", "k1"), ("/c", "r", "k2")]
    )
    assert "| `k1` | `/a`, `/b` | `wa` |" in body
    assert "| `k2` | `/c` | `r` |" in body


def test_pi_apt_timers_render_the_unit_and_time():
    body = h.render_pi_apt_timers([{"unit": "x.timer", "at": "01:02"}])
    assert "| `x.timer` | `01:02` |" in body


def test_pi_log2ram_renders_each_conf_key_with_its_var_and_value():
    d = {
        "optimize_pi_log2ram_size": "64M",
        "optimize_pi_log2ram_zram_backed": "true",
        "optimize_pi_log2ram_disk_size": "1G",
        "optimize_pi_log2ram_comp_alg": "zstd",
    }
    body = h.render_pi_log2ram(d)
    assert "| `SIZE` | `optimize_pi_log2ram_size` | `64M` |" in body
    assert "| `COMP_ALG` | `optimize_pi_log2ram_comp_alg` | `zstd` |" in body


def test_valheim_mods_list_enabled_before_disabled():
    body = h.render_valheim_mods([("Live", "1.0")], [("Off", "2.0")])
    assert body.index("`Live` | `1.0` | enabled") < body.index(
        "`Off` | `2.0` | disabled"
    )


def test_renovate_bounds_render_each_var_with_its_unit():
    body = h.render_renovate_bounds(
        {
            "renovate_agent_max_prs": 2,
            "renovate_agent_run_timeout_s": 60,
            "renovate_agent_unit_timeout": "9min",
            "renovate_agent_budget_usd": 7,
        }
    )
    assert "| `renovate_agent_run_timeout_s` | `60 s` |" in body
    assert "| `renovate_agent_budget_usd` | `7 USD` |" in body


def test_jellyfin_plugins_strip_the_image_digest_and_dash_a_missing_abi():
    body = h.render_jellyfin_plugins(
        "reg.example/jf:1.2-ls3@sha256:abcd",
        [{"name": "P", "version": "9", "target_abi": "—"}],
    )
    assert "`reg.example/jf:1.2-ls3`" in body
    assert "sha256" not in body
    assert "| P | `9` | — |" in body


# --- readers, against crafted text (accept, then reject) -------------------------------------

LB_DOC = """\
apiVersion: v1
kind: Service
metadata:
  name: demo-lan
spec:
  type: LoadBalancer
  externalTrafficPolicy: Local
"""


def test_a_loadbalancer_service_is_found_with_its_policy():
    assert h.parse_loadbalancers(LB_DOC, "demo") == [
        {"role": "demo", "service": "demo-lan", "etp": "Local"}
    ]


def test_a_loadbalancer_without_a_policy_reads_as_cluster():
    text = LB_DOC.replace("  externalTrafficPolicy: Local\n", "")
    assert h.parse_loadbalancers(text, "demo")[0]["etp"] == "Cluster"


def test_a_clusterip_service_is_not_a_loadbalancer():
    text = LB_DOC.replace("type: LoadBalancer", "type: ClusterIP")
    assert h.parse_loadbalancers(text, "demo") == []


def test_a_commented_loadbalancer_is_not_a_loadbalancer():
    text = LB_DOC.replace("  type: LoadBalancer", "  # type: LoadBalancer")
    assert h.parse_loadbalancers(text, "demo") == []


def test_only_the_loadbalancer_document_of_a_multidoc_template_is_found():
    clusterip = LB_DOC.replace("LoadBalancer", "ClusterIP").replace("demo-lan", "demo")
    found = h.parse_loadbalancers(clusterip + "---\n" + LB_DOC, "demo")
    assert [s["service"] for s in found] == ["demo-lan"]


def test_an_audit_watch_line_is_parsed():
    text = "      -w /etc/x -p wa -k kx\n      -a always,exit -S execve\n"
    assert h.parse_audit_watches(text) == [("/etc/x", "wa", "kx")]


def test_a_non_watch_audit_rule_is_ignored():
    assert h.parse_audit_watches("-a always,exit -F arch=b64 -S execve -k exec\n") == []


def test_a_commented_mod_entry_is_a_disabled_mod():
    text = "  # - name: Foo\n  #   version: 1.2.3\n  #   url: u\n"
    assert h.parse_disabled_mods(text) == [("Foo", "1.2.3")]


def test_a_live_mod_entry_is_not_a_disabled_mod():
    assert h.parse_disabled_mods("  - name: Foo\n    version: 1.2.3\n") == []


def test_a_plugin_is_found_by_its_version_var_with_its_abi():
    d = {
        "jellyfin_k8s_introskipper_version": "1.0",
        "jellyfin_k8s_introskipper_target_abi": "10.11.0.0",
        "jellyfin_k8s_image": "x:1",
    }
    assert h.read_jellyfin_plugins(d) == [
        {"name": "Intro Skipper", "version": "1.0", "target_abi": "10.11.0.0"}
    ]


# --- readers, against the real tree, naming members they must find ---------------------------


def test_the_tree_scan_finds_every_known_loadbalancer():
    found = {(s["role"], s["etp"]) for s in h.read_loadbalancers()}
    assert {
        ("traefik", "Local"),
        ("pihole", "Local"),
        ("mosquitto", "Local"),
        ("jellyfin", "Local"),
        ("wg-easy", "Local"),
        ("terraria", "Local"),
        ("valheim", "Local"),
    } <= found


def test_the_audit_tasks_still_carry_the_identity_watches():
    watches = h.parse_audit_watches(h.AUDIT_TASKS.read_text())
    assert ("/etc/sudoers", "wa", "actions") in watches
    assert {key for _, _, key in watches} >= {"identity", "sshd", "actions"}


def test_the_valheim_defaults_split_into_live_and_disabled_mods():
    live = {m["name"] for m in Estate().role_vars(h.VALHEIM_ROLE)["valheim_k8s_mods"]}
    disabled = {
        n
        for n, _ in h.parse_disabled_mods(
            (h.VALHEIM_ROLE / "defaults" / "main.yml").read_text()
        )
    }
    assert "Server_devcommands" in live
    assert "Jotunn" in disabled
    assert not live & disabled


def test_the_jellyfin_defaults_yield_all_five_plugins():
    plugins = h.read_jellyfin_plugins(Estate().role_vars(h.JELLYFIN_ROLE))
    assert {p["name"] for p in plugins} >= {
        "Ani-Sync",
        "Intro Skipper",
        "Webhook",
        "Merge Versions",
        "Media Cleaner",
    }
