"""The LAN facts (MetalLB VIPs, the LAN subnet, each host's address, the WireGuard client
subnet, and the cni0 gateway pair) must have exactly one YAML definition site. Everywhere
else, a `.j2` template, an Ansible task file, or a Python module reads the variable rather
than re-typing the literal.

A census swept every `192.168.`, `10.0.0.`, `10.42.` and `10.43.` hit across `ansible/`,
`scripts/` and `docs/` (94 files) and classified each one: a definition site, a correct
`{{ var }}` reference, a test/fixture value, prose, or a genuine duplicate. Three duplicates
were real and are fixed
in the same change that added this guard: `loki-homelab/templates/ingressroute.yaml.j2` and
netpol-baseline's own `networkpolicy-mosquitto.yaml.j2` / `networkpolicy-nut.yaml.j2` each
hardcoded the cni0 gateway pair instead of looping `netpol_baseline_node_cidrs`, which now
itself aliases the new `k3s_cni0_gateways` group_var — the fact loki-homelab (a different
role) needed to reach it too. A fourth fact, the WireGuard client subnet, had NO variable at
all (wg-easy v15 picks `10.8.0.0/24` internally; nothing in this repo assigns it) — three
comments quoted the bare literal, so `wg_client_subnet` was added to group_vars and the
comments now cite it by name.

What this guard does NOT cover, and why: the census's own grep pattern is broader than the six
named facts — `10.42.` and `10.43.` also match the k3s pod/Service CIDRs (`k3s_pod_cidr`,
`k3s_service_cidr`) and the per-service ClusterIPs, none of which are LAN facts. Those already
have their own single source (group_vars, or a role's own `defaults/main.yml`) and are out of
scope here; duplicating IN the pod CIDR under this guard's name would mean policing a second,
unrelated class of fact under a title that doesn't own it.

Two failure shapes, and both must fail, per this repo's established pattern for a guard that
finds its own subject by scanning text (`volume-claim`'s short-circuit and `image-smoke`'s
bare boot both shipped green while checking nothing): `flagged()` is the pure core, proven by
an `_is_clean` / `_is_flagged` pair below, and `test_the_census_actually_scanned_the_files_this_change_touched`
asserts the walk visits a NAMED set of paths rather than trusting an empty result.
"""

import re
from pathlib import Path

from _helpers import REPO

# --- the four LAN-fact literal shapes -----------------------------------------------------
#
# Deliberately narrower than the census's own `10.0.0.\|10.42.\|10.43.` sweep — see the module
# docstring for why the pod/Service CIDRs and per-service ClusterIPs are out of scope. A LAN
# octet immediately followed by `/` is a CIDR mention (`10.0.0.0/24`, `10.0.0.0/8`), which is
# either the `lan_subnet` definition itself or a supernet comment, not a duplicated host/VIP
# address — excluded so those don't need their own allowlist rows.
#
# `lan_octet` is a CLOSED set of the seven values group_vars/host_vars actually assign as a
# host address or MetalLB VIP — not every `10.0.0.*`. A bare digit pattern also matches every
# unrelated LAN device this repo happens to mention (a smart display, a Zigbee coordinator),
# and those are not one of the LAN facts this guard owns.
_LAN_OCTETS = ("139", "161", "215", "240", "241", "242", "243")
PATTERNS = {
    "staging_subnet": re.compile(r"192\.168\.140\.\d+"),
    "lan_octet": re.compile(r"10\.0\.0\.(?:" + "|".join(_LAN_OCTETS) + r")(?!\d)"),
    "wg_client_subnet": re.compile(r"10\.8\.0\.\d+"),
    "cni0_gateway": re.compile(r"10\.42\.[01]\.1/32"),
}

SCAN_ROOTS = ("ansible", "scripts", "docs")
SCAN_SUFFIXES = (".yml", ".yaml", ".j2", ".py", ".md")

# (path-prefix-or-exact-match regex, reason). A path matching ANY entry is exempt. Ordered
# roughly by how many files a rule covers, broadest first.
ALLOWLIST: list[tuple[str, str]] = [
    (r"docs/archive/", "superseded planning docs; excluded from the census itself too"),
    (r"(^|/)tests?/", "fixture/assertion value, not a source"),
    (r"(^|/)test_[^/]+\.py$", "fixture/assertion value, not a source"),
    (r"(^|/)_pi_health\.py$", "test helper"),
    (r"(^|/)_infra_map\.py$", "test helper"),
    (r"(^|/)conftest\.py$", "pytest fixture module"),
    (r"\.md$", "prose — a doc quotes a value, it does not define one"),
    (r"(^|/)CLAUDE\.md$", "prose"),
    (r"(^|/)SETUP\.md$", "prose"),
    (
        r"^ansible/inventory/group_vars/all\.yml$",
        "the source definition site for every fact this guard covers",
    ),
    (
        r"^ansible/inventory/host_vars/(daniel-box|daniel-pi|daniel-server|daniel-stage)\.yml$",
        "per-host source definitions; daniel-stage's MetalLB pool is a deliberately separate "
        "override, not a duplicate of the production one",
    ),
    (
        r"^ansible/inventory/host_vars/_example\.yml$",
        "placeholder template (`10.0.0.x`), not a real address",
    ),
    (
        r"^ansible/roles/k8s/netpol-baseline/defaults/main\.yml$",
        "netpol_baseline_node_cidrs now aliases k3s_cni0_gateways; "
        "netpol_baseline_obs_node_cidrs is a DELIBERATELY separate list per its own comment "
        "('carries its OWN node-CIDR list... widening it would re-scope all of them')",
    ),
    (
        r"^ansible/roles/k8s/monitor-bridge/files/bridge/config\.py$",
        "shipped runtime file (runs in-pod); no repo-side loader is reachable from there",
    ),
    (r"^docs/reference/hosts\.md$", "generated page; hook-enforced, no hand edits"),
    (
        r"^docs/assets/generated/fragments/lan-addresses\.md$",
        "generated fragment; gen_doc_fragments.py reads group_vars, not a hand copy",
    ),
    (
        r"^ansible/roles/setup/k3s/defaults/main\.yml$",
        "k3s_metallb_pool is the definition site for the ingress pool's own address range "
        "(10.0.0.241-10.0.0.250), a distinct fact from the six named LAN facts this guard "
        "tracks — not a duplicate of any single VIP or host address (#975 removed this "
        "file's other exemption, for k3s_server_join_url, by rewriting that literal to "
        "hostvars['daniel-box'].server_ip)",
    ),
    (
        r"^ansible/roles/setup/hypervisor/defaults/main\.yml$",
        "the source definition site for the hypervisor's own staging-net gateway/DHCP range "
        "(DECIDED comment above: chosen against a census of daniel-server's routes, not a "
        "copy of staging_net_cidr)",
    ),
    (
        r"^ansible/roles/k8s/traefik/templates/service\.yaml\.j2$",
        "comment citing the broad 10.0.0.0/8 RFC1918 supernet, not the /24 lan_subnet",
    ),
    (
        r"^scripts/dev/measure_rollout_gap\.py$",
        "CLI default for a manual diagnostic tool, overridable via --server; not a source",
    ),
]
_ALLOWLIST_RE = [(re.compile(pattern), reason) for pattern, reason in ALLOWLIST]


def _allowlisted(relpath: str) -> str | None:
    for regex, reason in _ALLOWLIST_RE:
        if regex.search(relpath):
            return reason
    return None


def _scan_files(repo: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for root_name in SCAN_ROOTS:
        for path in (repo / root_name).rglob("*"):
            if path.is_file() and path.suffix in SCAN_SUFFIXES:
                files[path.relative_to(repo).as_posix()] = path.read_text(
                    errors="replace"
                )
    return files


def _code_lines(text: str) -> list[tuple[int, str]]:
    """Every line that is not a `#` comment or inside a `{# ... #}` Jinja comment block.

    A comment can quote a value for narrative reasons ("VIP 10.0.0.244 since it went live")
    without re-deriving it as data a manifest would render differently if the source changed
    — that is what the census called (c). Only a value a template or task actually ASSIGNS is
    the duplicate this guard cares about.
    """
    lines = []
    in_jinja_comment = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if in_jinja_comment:
            if "#}" in line:
                in_jinja_comment = False
            continue
        if stripped.startswith("{#"):
            if "#}" not in line:
                in_jinja_comment = True
            continue
        if stripped.startswith("#"):
            continue
        lines.append((lineno, line))
    return lines


def flagged(files: dict[str, str]) -> list[str]:
    """Every `relpath: line: match` where a LAN-fact literal sits outside the allowlist.

    Pure over an in-memory `{relpath: text}` mapping so the red/green pair below can drive it
    without touching disk.
    """
    problems = []
    for relpath, text in sorted(files.items()):
        if _allowlisted(relpath):
            continue
        for lineno, line in _code_lines(text):
            for name, pattern in PATTERNS.items():
                m = pattern.search(line)
                if m:
                    problems.append(
                        f"{relpath}:{lineno}: {name} literal {m.group(0)!r}"
                    )
    return problems


# --- red-proof pair for the pure core -------------------------------------------------------


def test_a_variable_reference_is_clean():
    files = {
        "ansible/roles/k8s/foo/templates/x.yaml.j2": "cidr: {{ k3s_cni0_gateways }}\n"
    }
    assert flagged(files) == []


def test_a_duplicated_literal_is_flagged():
    files = {
        "ansible/roles/k8s/foo/templates/x.yaml.j2": "        - ipBlock:\n"
        "            cidr: 10.42.0.1/32\n"
    }
    problems = flagged(files)
    assert len(problems) == 1
    assert "cni0_gateway literal '10.42.0.1/32'" in problems[0]


def test_an_allowlisted_path_is_not_flagged_even_with_a_bare_literal():
    files = {
        "ansible/inventory/group_vars/all.yml": "k3s_metallb_ingress_vip: 10.0.0.240\n"
    }
    assert flagged(files) == []


# --- the real guard --------------------------------------------------------------------------


def test_no_lan_fact_literal_sits_outside_the_allowlist():
    problems = flagged(_scan_files(REPO))
    assert not problems, "\n".join(problems)


# --- non-vacuity: the walk must actually visit the files this change touched -----------------

CONVERTED_FILES = frozenset(
    {
        "ansible/inventory/group_vars/all.yml",
        "ansible/roles/k8s/netpol-baseline/defaults/main.yml",
        "ansible/roles/k8s/netpol-baseline/templates/networkpolicy-mosquitto.yaml.j2",
        "ansible/roles/k8s/netpol-baseline/templates/networkpolicy-nut.yaml.j2",
        "ansible/roles/k8s/loki-homelab/templates/ingressroute.yaml.j2",
        "ansible/roles/k8s/traefik/templates/static-config.yaml.j2",
        "ansible/roles/k8s/authelia/templates/config-secret.yaml.j2",
        "ansible/roles/setup/docker_install/tasks/install.yml",
        "docs/wireguard-private-homelab-access.md",
    }
)


def test_the_census_actually_scanned_the_files_this_change_touched():
    scanned = set(_scan_files(REPO))
    missing = CONVERTED_FILES - scanned
    assert not missing, f"walk did not visit: {missing}"
