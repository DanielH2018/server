"""The renderers behind the doc fragments: pure functions from plain values to markdown.

Every function here takes what a reader in `fragment_readers.py` returned — a dict of role
defaults, a tuple of prefixes, an int — and returns the body of one fragment. Nothing here
reads the tree, so a test pins the wording against literals it supplies itself rather than
against whatever the repo happens to hold today.

The provenance header is not a renderer's business: `gen_doc_fragments.header` prepends it,
because the marker names the entry point a reader has to run to regenerate the file.
"""


def _code_list(items: list[str]) -> str:
    return ", ".join(f"`{item}`" for item in items)


def render_longhorn_tiers(d: dict) -> str:
    """Renders the Longhorn backup-tier markdown table fragment.

    Args:
        d: the k3s role's defaults, carrying the longhorn tier and schedule tunables.
    """
    r2 = list(d["k3s_longhorn_r2_volumes"])
    weekly = list(d["k3s_longhorn_weekly_volumes"])
    nobackup = list(d["k3s_longhorn_nobackup_volumes"])
    minute, hour = str(d["k3s_longhorn_weekly_backup_minute_hour"]).split()
    armed = "armed" if d["k3s_longhorn_backup_armed"] else "**disarmed**"
    lines = [
        "| Tier | Target | Volumes | Schedule (daniel-box, UTC) | Retain |",
        "|---|---|---|---|---|",
        f"| Daily | R2 (`r2`) | {len(r2)} | `{d['k3s_longhorn_backup_cron']}` "
        f"| {d['k3s_longhorn_backup_retain']} |",
        f"| Weekly | B2 (`default`) | {len(weekly)}, one weekday each "
        f"| `{minute} {hour} * * <index mod 7>` | {d['k3s_longhorn_weekly_backup_retain']} |",
        f"| None | — | {len(nobackup)} listed, plus the `longhorn-nobackup` StorageClass | — | — |",
        "",
        f"Daily-tier volumes (`k3s_longhorn_r2_volumes`): {_code_list(r2)}.",
        "",
        f"B2 backups are {armed} (`k3s_longhorn_backup_armed`); the daily B2 budget is "
        f"{d['k3s_longhorn_daily_backup_budget']} backups (`k3s_longhorn_daily_backup_budget`).",
    ]
    return "\n".join(lines) + "\n"


def render_gitops_prefixes(setup: tuple, deploy: tuple, manual: tuple) -> str:
    """Renders the GitOps broad-change prefix classification table fragment.

    Args:
        setup: prefixes the deployer fast-forwards then applies via `initial_setup.yml`.
        deploy: prefixes the deployer fast-forwards then applies via a full `deploy.yml`.
        manual: prefixes the deployer never applies automatically.
    """
    rows = [
        ("Setup, scoped", setup, "_BROAD_SETUP_PREFIXES",
         "fast-forwards, then runs `initial_setup.yml --tags <name>`"),
        ("Deploy plane", deploy, "_BROAD_DEPLOY_PREFIXES",
         "fast-forwards, then runs a full `ansible/deploy.yml`"),
        ("Never applied here", manual, "_BROAD_MANUAL_PREFIXES",
         "alerts and returns **without fast-forwarding**"),
    ]  # fmt: skip
    lines = ["| Class | Prefixes | What the deployer does |", "|---|---|---|"]
    for label, prefixes, constant, action in rows:
        lines.append(
            f"| {label} | {_code_list(list(prefixes))} (`{constant}`) | {action} |"
        )
    return "\n".join(lines) + "\n"


def render_staging_subset(csv: str) -> str:
    names = sorted(n.strip() for n in csv.split(",") if n.strip())
    return (
        f"The subset as the deployer defaults it (`STAGING_SUBSET` in `gitops_deploy.py`; a "
        f"host's `config.env` can override it): {_code_list(names)} — {len(names)} services.\n"
    )


def render_staging_coverage(
    all_roles: list[str], subset: set[str], eligible: list[str]
) -> str:
    """Renders how much of the fleet the staging gate covers, and who it leaves unprotected.

    Args:
        all_roles: every role name under `ansible/roles/k8s/`.
        subset: `STAGING_SUBSET`, the roles the staging gate actually runs.
        eligible: role names declaring `k8s_autodeploy: true` — an image-pin bump can
            reach these unattended, so they are the ones a staging run would protect.
    """
    ungated_eligible = sorted(set(eligible) - subset)
    lines = [
        f"{len(subset)} of {len(all_roles)} k8s roles are staging-gated (`STAGING_SUBSET` "
        f"against every directory under `ansible/roles/k8s/`).",
        "",
        f"{len(ungated_eligible)} auto-deploy-eligible role(s) sit outside the gate — an "
        f"image-pin bump to any of these reaches production unattended with no staging run "
        f"first: {_code_list(ungated_eligible)}.",
    ]
    return "\n".join(lines) + "\n"


def render_autodeploy_coverage(
    eligible: list[str], denied: list[str], not_declaring: list[str]
) -> str:
    """Renders the k8s auto-deploy eligibility fragment.

    Args:
        eligible: role names declaring `k8s_autodeploy: true`.
        denied: role names declaring `k8s_autodeploy: false` — the denylist.
        not_declaring: role names declaring no stance (`SHARED_ROLES`, which deploys no
            service of its own).
    """
    lines = [
        "| Stance | Roles |",
        "|---|---|",
        f"| Eligible | {len(eligible)} |",
        f"| Denied | {len(denied)} |",
        f"| Not declaring | {len(not_declaring)} |",
        "",
        f"Denylisted (`k8s_autodeploy: false`): {_code_list(denied)}.",
    ]
    return "\n".join(lines) + "\n"


def render_staging_timeouts(gate_s: int, expect_s: int) -> str:
    """Renders the advisory wall-clock the staging gate can add to a tick.

    Args:
        gate_s: `STAGING_GATE_TIMEOUT_S`, staging's own deploy budget.
        expect_s: `STAGING_EXPECT_TIMEOUT_S`, the wait for the manifest queue.
    """
    return (
        f"Worst case the staging gate adds {gate_s + expect_s}s to a tick: "
        f"`STAGING_GATE_TIMEOUT_S` = {gate_s}s for staging's own deploy, plus "
        f"`STAGING_EXPECT_TIMEOUT_S` = {expect_s}s waiting for the manifest queue.\n"
    )


def render_crowdsec_agent_liveness(period_s: int) -> str:
    """Renders the CrowdSec node-agent's liveness probe interval.

    Args:
        period_s: `periodSeconds` on the node-agent DaemonSet's `cscli lapi status` probe.
    """
    return (
        f"The CrowdSec node-agent's `livenessProbe` runs `cscli lapi status` every "
        f"{period_s}s and restarts the container until registration lands.\n"
    )


def render_etcd_offbox_retention(retention: int) -> str:
    """Renders the off-box etcd snapshot retain count.

    Args:
        retention: `k3s_etcd_s3_retention`, how many R2 snapshots are kept.
    """
    return f"{retention} off-box etcd snapshots are retained on R2 (`k3s_etcd_s3_retention`).\n"


def render_traefik_ports(http_port: int, https_port: int) -> str:
    """Renders Traefik's pod-level ports, as opposed to the Service's 80/443.

    Args:
        http_port: `traefik_k8s_http_port`, the container's HTTP containerPort.
        https_port: `traefik_k8s_https_port`, the container's HTTPS containerPort.
    """
    return (
        f"Traefik's containerPorts are `{http_port}` (`traefik_k8s_http_port`) and "
        f"`{https_port}` (`traefik_k8s_https_port`) — a NetworkPolicy naming the Service's "
        f"80/443 admits nothing.\n"
    )


def render_staging_vm_sizing(memory_mib: int, vcpus: int, disk: str) -> str:
    """Renders the staging VM's resource budget.

    Args:
        memory_mib: `hypervisor_staging_vm_memory_mib`.
        vcpus: `hypervisor_staging_vm_vcpus`.
        disk: `hypervisor_staging_vm_disk_size`.
    """
    gib = memory_mib // 1024
    return (
        f"The staging VM is sized {gib} GB RAM (`hypervisor_staging_vm_memory_mib` = "
        f"{memory_mib}), {vcpus} vCPU (`hypervisor_staging_vm_vcpus`), {disk} disk "
        f"(`hypervisor_staging_vm_disk_size`).\n"
    )


def render_secret_tiers(tier_days: dict, lead_days: int, counts: dict[str, int]) -> str:
    """Renders the secret-rotation tier table fragment.

    Args:
        tier_days: tier name to rotation cadence in days, or None for no cadence.
        lead_days: `ROTATE_LEAD_DAYS`, the window the weekly auto rotation takes.
        counts: tier name to registered secret count.
    """
    lines = ["| Tier | Cadence | Registered |", "|---|---|---|"]
    for tier, days in tier_days.items():
        cadence = f"{days} d" if days is not None else "—"
        lines.append(f"| `{tier}` | {cadence} | {counts.get(tier, 0)} |")
    total = sum(counts.values())
    lines += [
        "",
        f"{total} secrets are registered. The weekly `auto` rotation takes anything due within "
        f"`ROTATE_LEAD_DAYS` = {lead_days} days.",
    ]
    return "\n".join(lines) + "\n"


def render_deadman_cadences(k3s: dict, pi_peer: dict, registry: dict) -> str:
    """One row per Healthchecks.io slug: the cron that pings it, assembled from its vars."""
    every_10 = f"{k3s['k3s_longhorn_backup_health_cron_minute']} * * * *"
    rows = [
        (
            "longhorn-backup-health",
            every_10,
            "`k3s_longhorn_backup_health_cron_minute`",
        ),
        (
            "daniel-box-disk-health",
            f"{k3s['k3s_disk_health_cron_minute']} * * * *",
            "`k3s_disk_health_cron_minute`",
        ),
        (
            "etcd-snapshot-offbox",
            f"{k3s['k3s_etcd_s3_cron_minute']} {k3s['k3s_etcd_s3_cron_hour']} * * *",
            "`k3s_etcd_s3_cron_hour` / `_minute`",
        ),
        (
            "manifest-prune-check",
            f"{k3s['k3s_manifest_prune_cron_minute']} {k3s['k3s_manifest_prune_cron_hour']} * * *",
            "`k3s_manifest_prune_cron_hour` / `_minute`",
        ),
        (
            "pi-peer-backup",
            str(pi_peer["pi_peer_backup_k8s_schedule"]),
            "`pi_peer_backup_k8s_schedule` (a k8s CronJob, not a host cron)",
        ),
        (
            "registry-gc",
            f"{registry['registry_k8s_gc_cron_minute']} {registry['registry_k8s_gc_cron_hour']} "
            f"* * {registry['registry_k8s_gc_cron_weekday']}",
            "`registry_k8s_gc_cron_weekday` / `_hour` / `_minute`",
        ),
        (
            "uptime-kuma-alive",
            every_10,
            "the `longhorn-backup-health` cron; the same script pings both",
        ),
    ]
    lines = ["| Check slug | Cron (daniel-box, UTC) | Set by |", "|---|---|---|"]
    lines += [f"| `{slug}` | `{cron}` | {source} |" for slug, cron, source in rows]
    return "\n".join(lines) + "\n"


def render_fail2ban_jails(jails: list[dict[str, str]]) -> str:
    lines = ["| Jail | Trigger | Ban |", "|---|---|---|"]
    for j in jails:
        lines.append(
            f"| `{j['jail']}` | {j['maxretry']} failures in {j['findtime']} "
            f"(`maxretry {j['maxretry']}`, `findtime {j['findtime']}`) | {j['bantime']} |"
        )
    return "\n".join(lines) + "\n"


def render_lan_addresses(
    ingress_vip: str,
    dns_vip: str,
    wg_port: str,
    pi_wg_port: str,
    lan_subnet: str,
    wg_client_subnet: str,
) -> str:
    """Renders the LAN address table fragment.

    Args:
        ingress_vip: the k3s MetalLB ingress VIP every `.local` service answers on.
        dns_vip: the Pi-hole DNS MetalLB VIP.
        wg_port: the WireGuard UDP port for wg-easy on daniel-box.
        pi_wg_port: the WireGuard UDP port for the Pi's LAN-only wg-easy.
        lan_subnet: the physical LAN subnet.
        wg_client_subnet: wg-easy's own client pool (not an Ansible input; see the
            `wg_client_subnet` comment in group_vars/all.yml).
    """
    rows = [
        ("k3s ingress VIP (MetalLB; every `.local` service answers here)", ingress_vip,
         "`k3s_metallb_ingress_vip`"),
        ("Pi-hole DNS VIP (MetalLB)", dns_vip, "`dns_k8s_vip`"),
        ("WireGuard UDP port, wg-easy on daniel-box", f"{wg_port}/udp",
         "`udp_port` on the k8s `wg-easy` entry in `host_vars/daniel-box.yml`"),
        ("WireGuard UDP port, the Pi's LAN-only wg-easy", f"{pi_wg_port}/udp",
         "`udp_port` on `wg-easy` in `host_vars/daniel-pi.yml`"),
        ("Home LAN subnet", lan_subnet, "`lan_subnet`"),
        ("WireGuard client subnet", wg_client_subnet, "`wg_client_subnet`"),
    ]  # fmt: skip
    lines = ["| Address | Value | Set by |", "|---|---|---|"]
    lines += [f"| {what} | `{value}` | {source} |" for what, value, source in rows]
    return "\n".join(lines) + "\n"
