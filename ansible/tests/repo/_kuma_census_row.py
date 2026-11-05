"""M6's two census rows: what still names Kuma, held to one frozen allowlist (exit criterion 3).

`KUMA_IDENTIFIERS` is the plan's precondition `git grep` as a row: an identifier that reads or
reaches Kuma. `KUMA_MENTIONS` holds every other `kuma` under `ansible/`, `hosts/`, `scripts/` and
`.claude/`. A test directory is outside the selector, which also keeps these rows and their red
fixtures out of it. Each allowlist entry names its file and its reason, and the list only shrinks:
a PR that removes a file's last mention deletes the file's entry, or the stale-allow check fails.

`docs/target-architecture/monitoring/m6-retire-kuma.md`, *Goal and exit criterion*, states the rule.
"""

import re

from _row_table import Census, Subject, lines_matching, tracked

# A citation of the plan, or of a file beside it, stays true after Kuma is gone.
PLAN_CITATION = re.compile(
    r"docs/target-architecture/monitoring/m6-[\w./-]*|m6-retire-kuma", re.I
)
# ADR 0026 keeps the `kuma-check-*` unit names and the directory of their down markers.
_KEPT_NAMES = re.compile(r"kuma-check-|/var/lib/kuma-check\b")

IDENTIFIERS = re.compile(
    r"/api/push/|kuma_push|KUMA_URL|KUMA_PUSH_|monitor_status"
    r"|entry_port\('uptime-kuma'\)|kuma-push-lib|kuma_ingress_monitors"
)

# Generated and keyed by test node ids, so they can name deleted tests. The docs-refresh cron
# rewrites both, and a stale-allow failure would turn master red on the cron's own commit.
GENERATED_RECORDS = frozenset(
    {
        "scripts/dev/pytest_shard_runner_weights.json",
        "scripts/dev/pytest_shard_weights.json",
    }
)

# Base64 ciphertext can spell `kuma` by chance, so a rotation could fail the row with nothing to
# reword. Every tracked SOPS file is secrets.yml or carries `.sops.` in its name.
_SECRETS_FILE = "ansible/vars/secrets.yml"


def _is_encrypted(rel: str) -> bool:
    return rel == _SECRETS_FILE or ".sops." in rel.rsplit("/", 1)[-1]


_C4B = "`kuma_ingress_monitors` until C-4b, which waits for D6, deletes the filter."
_REDACTION = "`/api/push/` is an output redaction rule, not a Kuma read (#7195)."

_IDENTIFIER_HOLDERS = {
    "ansible/filter_plugins/kuma_monitors.py": _C4B,
    "scripts/deploy_tools/narrow_lib/containers.py": _C4B,
    "scripts/lib/ansible_jinja_env.py": _C4B,
    "ansible/roles/k8s/loki-homelab/CLAUDE.md": _REDACTION,
    "ansible/roles/k8s/loki-homelab/templates/config/config.alloy.j2": _REDACTION,
    "scripts/diagnostics/probe_lib/core.py": _REDACTION,
    "hosts/components/claude-code/files/homelab_rootq.py": (
        "`/api/push/` is an output redaction rule, not a Kuma read."
    ),
    "scripts/secrets_mgmt/git_dates.py": (
        "`RETIRED` names retired keys by design, so it holds `kuma_push` inside key names."
    ),
}

_INERT_LABEL = (
    "The inert `org.homelab.kuma-docker-monitor` label: a label change recreates the container, "
    "so these go in the next Pi compose change (the plan's open question 7)."
)
_K3S_COMMENT = "A setup/k3s comment that names Kuma as a check's reader (#7147, #6882)."
_LIVE_NAME = (
    "A live name or matcher, such as `etcd_drill_full_kuma_interval_s`, `probe.py releases "
    "--kuma` or the webhook labelled Kuma: renaming it touches its readers (#7194)."
)
_R4_DELETES = "The render_records role, which R4 deletes (#7178)."
_SHIPPED_COMMENT = (
    "A comment in a file the role renders or ships names Kuma as history: rewording it changes "
    "the bytes that deploy, so it goes with the role's next change (#7194)."
)
_UNIT_FAMILY = "Names the `kuma-check` unit family without the prefix: ADR 0026 keeps the units (#7194)."

_PROSE_HOLDERS = {
    **dict.fromkeys(
        (
            "hosts/components/pi-compose/services/alloy/compose.yaml.j2",
            "hosts/components/pi-compose/services/autoheal/compose.yaml.j2",
            "hosts/components/pi-compose/services/docker-proxy/compose.yaml.j2",
            "hosts/components/pi-compose/services/wg-easy/compose.yaml.j2",
        ),
        _INERT_LABEL,
    ),
    **dict.fromkeys(
        ("ansible/roles/setup/k3s/tasks/health-crons.yml",),
        _K3S_COMMENT,
    ),
    **dict.fromkeys(
        (
            ".claude/hooks/hooklib/service_lines.py",
            "ansible/inventory/group_vars/all.yml",
            "ansible/roles/k8s/monitor-bridge/files/checks/notify.py",
            "ansible/roles/k8s/observability/files/rules/edge.yaml",
            "ansible/roles/setup/hypervisor/CLAUDE.md",
            "ansible/roles/setup/hypervisor/defaults/main.yml",
            "ansible/roles/setup/hypervisor/templates/etcd-restore-drill-vm.sh.j2",
            "ansible/roles/setup/k3s/templates/release-staleness-check.sh.j2",
            "scripts/diagnostics/probe_lib/cli_parser.py",
            "scripts/diagnostics/probe_lib/releases.py",
            "scripts/diagnostics/probe_lib/releases_format.py",
            "scripts/diagnostics/probe_lib/releases_render.py",
        ),
        _LIVE_NAME,
    ),
    **dict.fromkeys(
        (
            "ansible/roles/setup/render_records/CLAUDE.md",
            "ansible/roles/setup/render_records/defaults/main.yml",
            "ansible/roles/setup/render_records/files/render_records.py",
            "ansible/roles/setup/render_records/tasks/main.yml",
            "ansible/roles/setup/render_records/templates/config.env.j2",
        ),
        _R4_DELETES,
    ),
    **dict.fromkeys(
        (
            "ansible/deploy.yml",
            "ansible/filter_plugins/py_table.py",
            "ansible/filter_plugins/scrape_jobs.py",
            "ansible/inventory/host_vars/daniel-pi.yml",
            "ansible/roles/k8s/artifacts/files/artifact_meta.py",
            "ansible/roles/k8s/authelia/templates/config-secret.yaml.j2",
            "ansible/roles/k8s/autofix-bridge/defaults/main.yml",
            "ansible/roles/k8s/autofix-bridge/files/autofix.py",
            "ansible/roles/k8s/cloudflare-ddns/defaults/main.yml",
            "ansible/roles/k8s/cloudflare-ddns/tasks/main.yml",
            "ansible/roles/k8s/configarr/defaults/main.yml",
            "ansible/roles/k8s/configarr/files/configarr_health.py",
            "ansible/roles/k8s/configarr/files/configarr_lib/configarr_health_logic.py",
            "ansible/roles/k8s/configarr/files/configarr_lib/configarr_status.py",
            "ansible/roles/k8s/crowdsec/files/appsec_verify.py",
            "ansible/roles/k8s/crowdsec/files/crowdsec_jobs.py",
            "ansible/roles/k8s/crowdsec/files/home_allowlist.py",
            "ansible/roles/k8s/crowdsec/files/jobs_metrics.py",
            "ansible/roles/k8s/crowdsec/files/remote_allowlist.py",
            "ansible/roles/k8s/crowdsec/templates/jobs-configmap.yaml.j2",
            "ansible/roles/k8s/home-assistant/files/automations/system.yaml",
            "ansible/roles/k8s/home-assistant/files/input_datetime.yaml",
            "ansible/roles/k8s/janitorr/defaults/main.yml",
            "ansible/roles/k8s/janitorr/files/janitorr_health.py",
            "ansible/roles/k8s/jellyfin/defaults/main.yml",
            "ansible/roles/k8s/loki-homelab/files/rules/homelab-logs.yaml",
            "ansible/roles/k8s/loki-homelab/templates/ingressroute.yaml.j2",
            "ansible/roles/k8s/manifests/tasks/main.yml",
            "ansible/roles/k8s/manifests/tasks/render_record.yml",
            "ansible/roles/k8s/monitor-bridge/files/bridge/config_io.py",
            "ansible/roles/k8s/monitor-bridge/files/bridge/config_service.py",
            "ansible/roles/k8s/monitor-bridge/files/bridge/msgfmt.py",
            "ansible/roles/k8s/monitor-bridge/files/bridge/parsing.py",
            "ansible/roles/k8s/monitor-bridge/files/bridge/types.py",
            "ansible/roles/k8s/monitor-bridge/files/check.py",
            "ansible/roles/k8s/monitor-bridge/files/checks/b2.py",
            "ansible/roles/k8s/monitor-bridge/files/checks/host_edge.py",
            "ansible/roles/k8s/monitor-bridge/files/checks/host_thermal.py",
            "ansible/roles/k8s/monitor-bridge/files/checks/service.py",
            "ansible/roles/k8s/monitor-bridge/files/checks/wan.py",
            "ansible/roles/k8s/monitor-bridge/files/deploy_pipeline.py",
            "ansible/roles/k8s/monitor-bridge/files/verdict_export.py",
            "ansible/roles/k8s/monitor-bridge/files/verdicts/storage.py",
            "ansible/roles/k8s/monitor-bridge/templates/config/env.yaml.j2",
            "ansible/roles/k8s/monitor-bridge/templates/env-secret.yaml.j2",
            "ansible/roles/k8s/monitor-bridge/templates/service.yaml.j2",
            "ansible/roles/k8s/netpol-baseline/defaults/main.yml",
            "ansible/roles/k8s/netpol-baseline/templates/networkpolicy-terraria.yaml.j2",
            "ansible/roles/k8s/netpol-baseline/templates/networkpolicy-traefik.yaml.j2",
            "ansible/roles/k8s/observability/files/dashboards/Infrastructure/alert-history.json",
            "ansible/roles/k8s/observability/files/rules/alerting-meta.yaml",
            "ansible/roles/k8s/observability/files/rules/bridge-exporter.yaml",
            "ansible/roles/k8s/observability/files/rules/container-checks.yaml",
            "ansible/roles/k8s/observability/files/rules/deploy-pipeline.yaml",
            "ansible/roles/k8s/observability/files/rules/extra-probes.yaml",
            "ansible/roles/k8s/observability/files/rules/gitops-checks.yaml",
            "ansible/roles/k8s/observability/files/rules/host-checks.yaml",
            "ansible/roles/k8s/observability/files/rules/hosts.yaml",
            "ansible/roles/k8s/observability/files/rules/ingress-probes.yaml",
            "ansible/roles/k8s/observability/files/rules/initial-setup-checks.yaml",
            "ansible/roles/k8s/observability/files/rules/log-pipeline.yaml",
            "ansible/roles/k8s/observability/files/rules/loki-homelab.yaml",
            "ansible/roles/k8s/observability/files/rules/other-setup-checks.yaml",
            "ansible/roles/k8s/observability/files/rules/pi-checks.yaml",
            "ansible/roles/k8s/observability/files/rules/pod-producers.yaml",
            "ansible/roles/k8s/observability/files/rules/pods.yaml",
            "ansible/roles/k8s/observability/files/rules/registry.yaml",
            "ansible/roles/k8s/observability/files/rules/scrape-targets.yaml",
            "ansible/roles/k8s/observability/files/rules/storage.yaml",
            "ansible/roles/k8s/observability/templates/alertmanager-secret.yaml.j2",
            "ansible/roles/k8s/observability/templates/prometheus.yaml.j2",
            "ansible/roles/k8s/pi-peer-backup/files/pull-pi-peers.sh",
            "ansible/roles/k8s/pi-peer-backup/templates/secret.yaml.j2",
            "ansible/roles/k8s/qbittorrent/defaults/main.yml",
            "ansible/roles/k8s/qbittorrent/files/prefs_check.py",
            "ansible/roles/k8s/qbittorrent/templates/deployment.yaml.j2",
            "ansible/roles/setup/common/tasks/release_bin.yml",
            "ansible/roles/setup/fake_remux/tasks/main.yml",
            "ansible/roles/setup/fake_remux/templates/fake-remux-health.sh.j2",
            "ansible/roles/setup/gitops_deploy/templates/github-interaction-limit.sh.j2",
            "ansible/roles/setup/gitops_deploy/templates/github-ruleset-drift.sh.j2",
            "ansible/roles/setup/hypervisor/tasks/etcd_drill.yml",
            "ansible/roles/setup/initial_setup/tasks/crons.yml",
            "ansible/roles/setup/initial_setup/templates/aide-check.sh.j2",
            "ansible/roles/setup/initial_setup/templates/loki-read-route-health.sh.j2",
            "ansible/roles/setup/initial_setup/templates/rkhunter-check.sh.j2",
            "ansible/roles/setup/initial_setup/templates/setup-drift-check.sh.j2",
            "ansible/roles/setup/k8s_host_files/defaults/main.yml",
            "ansible/roles/setup/k8s_host_files/templates/registry-gc.sh.j2",
            "ansible/roles/setup/nut_host/defaults/main.yml",
            "ansible/roles/setup/nut_host/templates/ups-secondary-health.sh.j2",
            "ansible/roles/setup/renovate_agent/files/renovate_agent.py",
            "ansible/templates/git-tree-lock.j2",
            "ansible/templates/ingressroute.yml.j2",
            "scripts/deploy_tools/prune_releases.py",
        ),
        _SHIPPED_COMMENT,
    ),
    **dict.fromkeys(
        (
            "ansible/roles/setup/common/tasks/check_timer.yml",
            "ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_cross_role.py",
            "ansible/roles/setup/gitops_deploy/tasks/teardown.yml",
        ),
        _UNIT_FAMILY,
    ),
}


def census_files() -> list[str]:
    selected = tracked(
        "ansible", "hosts", "scripts", ".claude", ":!*/tests/*", ":!*/test_*"
    )
    return [
        rel
        for rel in selected
        if rel not in GENERATED_RECORDS and not _is_encrypted(rel)
    ]


def kuma_mentions(subject: Subject) -> list[str]:
    """Each line that names `kuma` once the plan's citations and the kept names are gone."""
    return [
        f"line {n}: {line.strip()}"
        for n, line in enumerate(subject.text.splitlines(), 1)
        if "kuma" in _KEPT_NAMES.sub("", PLAN_CITATION.sub("", line)).lower()
    ]


_identifier_hits = lines_matching(IDENTIFIERS)

KUMA_IDENTIFIERS = Census(
    name="kuma-identifiers-stay-on-their-allowlist",
    reason=(
        "Nothing reads Kuma: M6 deleted it, so a new push, status read or port lookup reaches "
        "nothing. Each file left here holds these identifiers for the reason its entry names. "
        "Removing a file's last identifier means deleting its entry."
    ),
    files=census_files,
    offence=_identifier_hits,
    red=(
        Subject("a.sh", "source /usr/local/lib/kuma-push-lib.sh\n"),
        Subject("a.py", "import kuma_push\n"),
        Subject("a.py", 'url = f"{base}/api/push/{token}"\n'),
        Subject("a.py", 'read = probe("monitor_status")\n'),
        Subject("a.env", "KUMA_URL=http://x\n"),
        Subject("a.env", "KUMA_PUSH_URL=http://x\n"),
        Subject("a.j2", "{{ containers_list | kuma_ingress_monitors }}\n"),
        Subject("a.j2", "{{ entry_port('uptime-kuma') }}\n"),
    ),
    green=(
        Subject("a.sh", "write_check_textfile up\n"),
        Subject("a.md", "kuma-check-loki-read-route.timer\n"),
    ),
    allow=_IDENTIFIER_HOLDERS,
    count=lambda subject: len(_identifier_hits(subject)),
    must_find=frozenset(
        {
            "hosts/components/claude-code/files/homelab_rootq.py",
            "scripts/secrets_mgmt/git_dates.py",
        }
    ),
)

KUMA_MENTIONS = Census(
    name="kuma-mentions-stay-on-their-allowlist",
    reason=(
        "M6 retired Kuma, so a new mention describes something that no longer exists. Say what "
        "pages now. To name the plan, cite it: a citation is stripped first. Rewording a file's "
        "last mention means deleting its entry from `_PROSE_HOLDERS`."
    ),
    files=census_files,
    offence=kuma_mentions,
    red=(
        Subject("a.py", "kuma_url = x  # m6-retire-kuma.md\n"),
        Subject("a.md", "This pages through Uptime Kuma.\n"),
        Subject("a.env", "KUMA_HOST=x\n"),
    ),
    green=(
        Subject("a.py", "# see m6-retire-kuma.md, C-6\n"),
        Subject(
            "a.py", "# docs/target-architecture/monitoring/m6-retire-kuma.md#design\n"
        ),
        Subject("a.md", "`systemctl status kuma-check-loki-read-route`\n"),
        Subject(
            "a.j2", "mkdir -p /var/lib/kuma-check && touch /var/lib/kuma-check/x.down\n"
        ),
    ),
    allow=_IDENTIFIER_HOLDERS | _PROSE_HOLDERS,
    count=lambda subject: len(kuma_mentions(subject)),
    must_find=frozenset({"scripts/secrets_mgmt/git_dates.py"}),
)
