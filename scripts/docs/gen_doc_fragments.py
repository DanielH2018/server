#!/usr/bin/env python3
"""Generate the fact tables the hand-written docs transclude.

WHY FRAGMENTS. The reference pages under docs/reference/ are generated whole, so a tunable
they show is re-read from the tree on every docs-refresh run. The operations pages are
prose, and prose quotes the same tunables: a retain count, the broad-prefix classes, the
staging subset. Nothing regenerated those, and on 2026-09-02 five such figures were stale.
Moving a whole runbook into a generator would put its prose in Python; leaving it alone
leaves the tables to rot. A fragment is the seam between the two: the generator emits ONLY
the table, and the page pulls it in with a `pymdownx.snippets` include::

    --8<-- "assets/generated/fragments/longhorn-tiers.md"

The prose around it stays hand-written. mkdocs.yml enables the extension with
`check_paths: true`, so an include naming a fragment that does not exist fails the strict
build rather than rendering the include line as text.

WHAT A FRAGMENT CARRIES. No frontmatter -- a snippet is spliced into the page verbatim, so
a `---` block would render as a rule and three lines of text. The provenance is an HTML
comment on the first line, and it carries the literal `generated_from:` because that string
is how .claude/hooks/block-protected-edits.py tells a generated page from a hand-written
one. No timestamp either: docs_provenance.write_if_body_changed compares the whole file
when there is no frontmatter, and a stamp would make every run a rewrite and every cron
run a commit. When a fragment's content last changed is its git history.

STATIC PARSING ONLY, like every generator here. Role defaults are read with yaml.safe_load
and Python constants with `ast`, never by importing the deployer or the rotation tool --
both bootstrap sys.path and read the environment on import. The k8s auto-deploy stance is
the one exception: `scripts/dev/k8s_autodeploy_counts.py` and the `k8s_autodeploy` filter
plugin it wraps do neither -- they only walk role directories and `yaml.safe_load` their
defaults -- so this script imports them rather than re-deriving the same partition.

WHERE THE PIECES LIVE. `fragment_readers.py` parses the tree, `fragment_renderers.py` turns
what a reader returned into markdown, and this file holds the seam: the source paths, the
provenance header, one `_<name>()` collector per fragment, and `FRAGMENTS`. `SELF` and the
source-path constants stay here because the header bakes `SELF` into every fragment, so the
marker names the entry point a reader has to run rather than a module they cannot invoke.

Every fragment must be included by at least one page and every include must name a
fragment this script emits; scripts/docs/tests/test_gen_doc_fragments.py checks both
directions, so a fragment cannot go dead and a page cannot include a name nobody writes.

Usage::

    uv run python scripts/docs/gen_doc_fragments.py --out-dir docs/assets/generated/fragments
"""

from __future__ import annotations

import argparse
import sys as _sys
from collections.abc import Callable
from pathlib import Path as _Path


# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from dev.k8s_autodeploy_counts import all_role_names, autodeploy_stances
from fragment_readers import (
    config_default,
    container_udp_port,
    module_constant,
    parse_jails,
    registry_counts,
    role_defaults,
)
from fragment_renderers import (
    render_autodeploy_coverage,
    render_crowdsec_agent_liveness,
    render_deadman_cadences,
    render_etcd_offbox_retention,
    render_fail2ban_jails,
    render_gitops_prefixes,
    render_lan_addresses,
    render_longhorn_tiers,
    render_secret_tiers,
    render_staging_coverage,
    render_staging_subset,
    render_staging_timeouts,
    render_staging_vm_sizing,
    render_traefik_ports,
)
from lib.docs_provenance import write_if_body_changed
from lib.repo_paths import REPO

SELF = "scripts/docs/gen_doc_fragments.py"
DEFAULT_OUT_DIR = "docs/assets/generated/fragments"

K3S_DEFAULTS = REPO / "ansible/roles/setup/k3s/defaults/main.yml"
GITOPS_DEFAULTS = REPO / "ansible/roles/setup/gitops_deploy/defaults/main.yml"
DEPLOY_CHANGES = REPO / "ansible/roles/setup/gitops_deploy/files/deploy_changes.py"
GITOPS_DEPLOY = REPO / "ansible/roles/setup/gitops_deploy/files/gitops_deploy.py"
SECRET_ROTATION = REPO / "scripts/secrets_mgmt/secret_rotation.py"
SECRET_REGISTRY = REPO / "ansible/secret_rotation.yml"
PI_PEER_DEFAULTS = REPO / "ansible/roles/k8s/pi-peer-backup/defaults/main.yml"
REGISTRY_DEFAULTS = REPO / "ansible/roles/k8s/registry/defaults/main.yml"
FAIL2BAN_CONF = (
    REPO / "ansible/roles/setup/initial_setup/templates/fail2ban_homelab.conf.j2"
)
GROUP_VARS = REPO / "ansible/inventory/group_vars/all.yml"
HOST_VARS = REPO / "ansible/inventory/host_vars"
TRAEFIK_DEFAULTS = REPO / "ansible/roles/k8s/traefik/defaults/main.yml"
HYPERVISOR_DEFAULTS = REPO / "ansible/roles/setup/hypervisor/defaults/main.yml"


def header(sources: list[str]) -> str:
    """The first line of every fragment: provenance the hook can read, in a comment."""
    return (
        f"<!-- generated_from: {SELF} -- do not edit. Regenerated by the docs-refresh cron "
        f"from {', '.join(sources)}; change the source. -->\n"
    )


# --- the fragment set --------------------------------------------------------------------


def _longhorn() -> tuple[str, list[str]]:
    return render_longhorn_tiers(role_defaults(K3S_DEFAULTS)), [
        "ansible/roles/setup/k3s/defaults/main.yml"
    ]


def _gitops() -> tuple[str, list[str]]:
    return render_gitops_prefixes(
        module_constant(DEPLOY_CHANGES, "_BROAD_SETUP_PREFIXES"),
        module_constant(DEPLOY_CHANGES, "_BROAD_DEPLOY_PREFIXES"),
        module_constant(DEPLOY_CHANGES, "_BROAD_MANUAL_PREFIXES"),
    ), ["ansible/roles/setup/gitops_deploy/files/deploy_changes.py"]


def _staging() -> tuple[str, list[str]]:
    return render_staging_subset(config_default(GITOPS_DEPLOY, "STAGING_SUBSET")), [
        "ansible/roles/setup/gitops_deploy/files/gitops_deploy.py"
    ]


def _staging_coverage() -> tuple[str, list[str]]:
    eligible, _denied, _not_declaring = autodeploy_stances()
    subset = {
        n.strip()
        for n in config_default(GITOPS_DEPLOY, "STAGING_SUBSET").split(",")
        if n.strip()
    }
    return render_staging_coverage(all_role_names(), subset, eligible), [
        "ansible/roles/k8s/ (role directories)",
        "ansible/roles/setup/gitops_deploy/files/gitops_deploy.py",
        "ansible/filter_plugins/k8s_autodeploy.py",
    ]


def _autodeploy_coverage() -> tuple[str, list[str]]:
    eligible, denied, not_declaring = autodeploy_stances()
    return render_autodeploy_coverage(eligible, denied, not_declaring), [
        "scripts/dev/k8s_autodeploy_counts.py",
        "ansible/filter_plugins/k8s_autodeploy.py",
    ]


def _staging_timeouts() -> tuple[str, list[str]]:
    return render_staging_timeouts(
        int(config_default(GITOPS_DEPLOY, "STAGING_GATE_TIMEOUT_S")),
        int(config_default(GITOPS_DEPLOY, "STAGING_EXPECT_TIMEOUT_S")),
    ), ["ansible/roles/setup/gitops_deploy/files/gitops_deploy.py"]


def _crowdsec_agent_liveness() -> tuple[str, list[str]]:
    source = "ansible/roles/k8s/crowdsec/templates/node-agent-daemonset.yaml.j2"
    text = (REPO / source).read_text()
    after_probe = text[text.index("livenessProbe:") :]
    period_line = next(
        line for line in after_probe.splitlines() if "periodSeconds:" in line
    )
    period_s = int(period_line.split(":")[1].strip())
    return render_crowdsec_agent_liveness(period_s), [source]


def _etcd_offbox_retention() -> tuple[str, list[str]]:
    retention = role_defaults(K3S_DEFAULTS)["k3s_etcd_s3_retention"]
    return render_etcd_offbox_retention(retention), [
        "ansible/roles/setup/k3s/defaults/main.yml"
    ]


def _traefik_ports() -> tuple[str, list[str]]:
    d = role_defaults(TRAEFIK_DEFAULTS)
    return render_traefik_ports(
        d["traefik_k8s_http_port"], d["traefik_k8s_https_port"]
    ), ["ansible/roles/k8s/traefik/defaults/main.yml"]


def _staging_vm_sizing() -> tuple[str, list[str]]:
    d = role_defaults(HYPERVISOR_DEFAULTS)
    return render_staging_vm_sizing(
        d["hypervisor_staging_vm_memory_mib"],
        d["hypervisor_staging_vm_vcpus"],
        d["hypervisor_staging_vm_disk_size"],
    ), ["ansible/roles/setup/hypervisor/defaults/main.yml"]


def _deadman() -> tuple[str, list[str]]:
    return render_deadman_cadences(
        role_defaults(K3S_DEFAULTS),
        role_defaults(PI_PEER_DEFAULTS),
        role_defaults(REGISTRY_DEFAULTS),
    ), [
        "ansible/roles/setup/k3s/defaults/main.yml",
        "ansible/roles/k8s/pi-peer-backup/defaults/main.yml",
        "ansible/roles/k8s/registry/defaults/main.yml",
    ]


def _fail2ban() -> tuple[str, list[str]]:
    return render_fail2ban_jails(parse_jails(FAIL2BAN_CONF.read_text())), [
        "ansible/roles/setup/initial_setup/templates/fail2ban_homelab.conf.j2"
    ]


def _lan() -> tuple[str, list[str]]:
    group = role_defaults(GROUP_VARS)
    return render_lan_addresses(
        str(group["k3s_metallb_ingress_vip"]),
        str(group["dns_k8s_vip"]),
        container_udp_port(role_defaults(HOST_VARS / "daniel-box.yml"), "wg-easy"),
        container_udp_port(role_defaults(HOST_VARS / "daniel-pi.yml"), "wg-easy"),
        str(group["lan_subnet"]),
        str(group["wg_client_subnet"]),
    ), [
        "ansible/inventory/group_vars/all.yml",
        "ansible/inventory/host_vars/daniel-box.yml",
        "ansible/inventory/host_vars/daniel-pi.yml",
    ]


def _secrets() -> tuple[str, list[str]]:
    return render_secret_tiers(
        module_constant(SECRET_ROTATION, "TIER_DAYS"),
        module_constant(SECRET_ROTATION, "ROTATE_LEAD_DAYS"),
        registry_counts(SECRET_REGISTRY),
    ), ["scripts/secrets_mgmt/secret_rotation.py", "ansible/secret_rotation.yml"]


# name -> () -> (body, sources). The name is the file stem a page includes.
FRAGMENTS: dict[str, Callable[[], tuple[str, list[str]]]] = {
    "longhorn-tiers": _longhorn,
    "broad-prefixes": _gitops,
    "staging-subset": _staging,
    "staging-coverage": _staging_coverage,
    "autodeploy-coverage": _autodeploy_coverage,
    "staging-timeouts": _staging_timeouts,
    "node-agent-liveness": _crowdsec_agent_liveness,
    "etcd-offbox-retention": _etcd_offbox_retention,
    "traefik-ports": _traefik_ports,
    "staging-vm-sizing": _staging_vm_sizing,
    "secret-tiers": _secrets,
    "deadman-cadences": _deadman,
    "fail2ban-jails": _fail2ban,
    "lan-addresses": _lan,
}


def write_fragments(out_dir: _Path) -> int:
    """Write every fragment whose content changed. Returns how many were written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for name, build in FRAGMENTS.items():
        body, sources = build()
        if write_if_body_changed(out_dir / f"{name}.md", header(sources) + body):
            written += 1
    return written


def main(argv: list[str] | None = None) -> int:
    """Entry point: writes every fragment and prints how many changed.

    Args:
        argv: command-line arguments, or None to use `sys.argv`.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out-dir", default=DEFAULT_OUT_DIR, help="where the fragments go"
    )
    args = parser.parse_args(argv)
    out_dir = _Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = REPO / out_dir
    written = write_fragments(out_dir)
    print(
        f"gen_doc_fragments: {len(FRAGMENTS)} fragment(s), {written} written -> {out_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
