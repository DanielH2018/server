"""The rows `test_k8s_host_files_rows.py` holds `setup/k8s_host_files` to, by task file.

`EXPECTED_ROWS` names each row a D5 conversion PR moved, by task file and task name, with the
module and the whole argument map it must keep. A looped row's path reads `item`, so
`EXPECTED_LOOPS` pins its loop too. The tables sit in their own module because the test module
reached the cap for a test module.
"""

# crowdsec's absent arms for the crons H2.4b and H2.4c retired (#5742). They run on daniel-box,
# the play's host, outside the file's daniel-pi block.
CROWDSEC_ARMS: dict[str, tuple[str, dict[str, str | bool]]] = {
    f"Remove the retired {label} cron": (
        "ansible.builtin.cron",
        {
            "name": f"CrowdSec {cron}",
            "user": "root",
            "cron_file": f"crowdsec-{cron_file}",
            "state": "absent",
        },
    )
    for label, cron, cron_file in (
        ("remote-allowlist", "remote allowlist", "remote-allowlist"),
        ("bouncer-prune", "bouncer prune", "bouncer-prune"),
        ("AppSec verify", "AppSec verify", "appsec-verify"),
        ("home allowlist", "home allowlist", "home-allowlist"),
    )
} | {
    "Remove the retired crowdsec cron scripts from the host": (
        "ansible.builtin.file",
        {"path": "{{ item }}", "state": "absent"},
    ),
}

# {task file: {task name: (module, its args)}}, one file per source role. The args are the
# task's whole argument map, so an owner, group or mode change fails the row as well as a path:
# docs' directory exists to be owned by sys_user, which the docs-refresh build writes as.
EXPECTED_ROWS: dict[str, dict[str, tuple[str, dict[str, str | bool]]]] = {
    "artifacts.yml": {
        "Ensure the local artifacts directory exists": (
            "ansible.builtin.file",
            {
                "path": "{{ artifacts_host_dir }}",
                "state": "directory",
                "owner": "{{ sys_user }}",
                "group": "{{ sys_user }}",
                "mode": "0750",
            },
        ),
        "Ensure a peer directory exists for every synced host": (
            "ansible.builtin.file",
            {
                "path": "{{ artifacts_peer_dir }}/{{ item.name }}",
                "state": "directory",
                "owner": "{{ sys_user }}",
                "group": "{{ sys_user }}",
                "mode": "0750",
            },
        ),
        # 0755, not 0750: the agent user's prune reads the star list as `claude`.
        "Ensure the star list directory exists": (
            "ansible.builtin.file",
            {
                "path": "{{ artifacts_stars_dir }}",
                "state": "directory",
                "owner": "{{ sys_user }}",
                "group": "{{ sys_user }}",
                "mode": "0755",
            },
        ),
    },
    "configarr.yml": {
        "Remove the retired configarr sync heartbeat cron": (
            "ansible.builtin.cron",
            {
                "name": "configarr sync health",
                "user": "{{ sys_user }}",
                "cron_file": "configarr-health",
                "state": "absent",
            },
        ),
        "Remove the retired configarr health reader from the host": (
            "ansible.builtin.file",
            {"path": "{{ item }}", "state": "absent"},
        ),
    },
    "docs.yml": {
        "Ensure the built docs site directory exists": (
            "ansible.builtin.file",
            {
                "path": "{{ docs_host_site_dir }}",
                "state": "directory",
                "owner": "{{ sys_user }}",
                "group": "{{ sys_user }}",
                "mode": "0755",
            },
        ),
    },
    # The wrapper first, then the key pinned to it: the reverse order leaves a forced command
    # that does not exist. Both run on daniel-pi through the block's `delegate_to`.
    "pi-peer-backup.yml": {
        "Install the forced-command wrapper on daniel-pi": (
            "ansible.builtin.copy",
            {
                "src": "pi-peer-backup-shell.sh",
                "dest": "/usr/local/bin/pi-peer-backup-shell",
                "owner": "root",
                "group": "root",
                "mode": "0755",
            },
        ),
        "Authorize the pull key on daniel-pi": (
            "ansible.posix.authorized_key",
            {
                "user": "{{ hostvars['daniel-pi'].ansible_user | default('ubuntu') }}",
                "key": "{{ lookup('file', 'pi-peer-backup.pub') }}",
                "key_options": (
                    'restrict,command="/usr/local/bin/pi-peer-backup-shell '
                    '{{ pi_peer_backup_src_dir }}"'
                ),
                "state": "present",
            },
        ),
    },
    # The edge probe rows run on daniel-pi through the block's `delegate_to`.
    "crowdsec.yml": {
        **CROWDSEC_ARMS,
        "Install the edge probe script on daniel-pi": (
            "ansible.builtin.copy",
            {
                "src": "crowdsec-edge-probe.sh",
                "dest": "/usr/local/bin/crowdsec-edge-probe",
                "owner": "root",
                "group": "root",
                "mode": "0755",
            },
        ),
        "Install the edge probe service on daniel-pi": (
            "ansible.builtin.template",
            {
                "src": "crowdsec-edge-probe.service.j2",
                "dest": "/etc/systemd/system/crowdsec-edge-probe.service",
                "owner": "root",
                "group": "root",
                "mode": "0644",
            },
        ),
        "Install the edge probe timer on daniel-pi": (
            "ansible.builtin.template",
            {
                "src": "crowdsec-edge-probe.timer.j2",
                "dest": "/etc/systemd/system/crowdsec-edge-probe.timer",
                "owner": "root",
                "group": "root",
                "mode": "0644",
            },
        ),
        "Enable the edge probe timer on daniel-pi": (
            "ansible.builtin.systemd",
            {
                "name": "crowdsec-edge-probe.timer",
                "enabled": True,
                "state": "started",
                "daemon_reload": True,
            },
        ),
    },
    # No mode, on purpose: the library keeps the directory modes it came with.
    "media-volume.yml": {
        "Create the media volume directories on the node": (
            "ansible.builtin.file",
            {
                "path": "{{ item }}",
                "state": "directory",
                "owner": "{{ puid }}",
                "group": "{{ pgid }}",
            },
        ),
    },
    # An absent arm, so it carries no owner or mode.
    "observability.yml": {
        "Remove the retired dashboard staging trees": (
            "ansible.builtin.file",
            {"path": "{{ item }}", "state": "absent"},
        ),
    },
    # Absent arms: the whole map pins `state: absent`, so an arm turned back into an install
    # fails here as well as in the retired-cron census.
    "traefik.yml": {
        "Remove the Cloudflare IP drift cron": (
            "ansible.builtin.cron",
            {
                "name": "Cloudflare IP drift",
                "user": "root",
                "cron_file": "cloudflare-ip-drift",
                "state": "absent",
            },
        ),
        "Remove the Cloudflare IP drift script": (
            "ansible.builtin.file",
            {"path": "/usr/local/bin/cloudflare-ip-drift.sh", "state": "absent"},
        ),
    },
    # The absent arm for the push store's GC manifest, which nothing reads since its retirement.
    "registry.yml": {
        "Remove the retired push store's garbage-collection job manifest": (
            "ansible.builtin.file",
            {
                "path": "/etc/rancher/k3s/manifests/registry-jobs/gc-job.yaml",
                "state": "absent",
            },
        ),
    },
    # The absent arms H2.5c left for the retired health reader's host cron, its code and the
    # drift fragment it stamped.
    "janitorr.yml": {
        "Remove the retired janitorr error heartbeat cron": (
            "ansible.builtin.cron",
            {
                "name": "janitorr error health",
                "user": "{{ sys_user }}",
                "cron_file": "janitorr-health",
                "state": "absent",
            },
        ),
        "Remove the retired janitorr health reader from the host": (
            "ansible.builtin.file",
            {"path": "{{ item }}", "state": "absent"},
        ),
    },
    # The absent arm for the k8s/volume-claim staging directory autokuma-data left behind.
    "uptime-kuma.yml": {
        "Remove the k8s/volume-claim staging directory autokuma-data left behind": (
            "ansible.builtin.file",
            {"path": "/etc/rancher/k3s/manifests/autokuma-claims", "state": "absent"},
        ),
    },
    # The absent arms H2.5d left for the retired prefs drift check's host cron and script.
    "qbittorrent.yml": {
        "Remove the retired qbittorrent prefs drift check cron": (
            "ansible.builtin.cron",
            {
                "name": "qbittorrent prefs drift check",
                "user": "{{ sys_user }}",
                "cron_file": "qbittorrent-prefs-check",
                "state": "absent",
            },
        ),
        "Remove the retired qbittorrent prefs-check script from the host": (
            "ansible.builtin.file",
            {"path": "{{ item }}", "state": "absent"},
        ),
    },
}

# {(task file, task name): its `loop`} for every looped row. A row absent here must not loop.
# media-volume's hardlink probe links between media/ and torrents/, so both must exist.
# configarr's arm removes three host paths.
EXPECTED_LOOPS: dict[tuple[str, str], object] = {
    (
        "artifacts.yml",
        "Ensure a peer directory exists for every synced host",
    ): "{{ artifacts_peer_sources }}",
    ("configarr.yml", "Remove the retired configarr health reader from the host"): [
        "/usr/local/bin/configarr-health.sh",
        "/opt/configarr-health",
        "/var/lib/homelab/setup-deployed-manifest.d/configarr",
    ],
    ("media-volume.yml", "Create the media volume directories on the node"): [
        "{{ media_volume_host_path }}",
        "{{ media_volume_host_path }}/media",
        "{{ media_volume_host_path }}/torrents",
    ],
    ("observability.yml", "Remove the retired dashboard staging trees"): [
        "/etc/rancher/k3s/dashboards",
        "/etc/rancher/k3s/dashboards-annotated",
        "/etc/rancher/k3s/dashboard-manifests",
    ],
    # The fragment as well as the code: a fragment left behind turns setup drift DOWN.
    ("janitorr.yml", "Remove the retired janitorr health reader from the host"): [
        "/usr/local/bin/janitorr-health.sh",
        "/opt/janitorr-health",
        "/var/lib/homelab/setup-deployed-manifest.d/janitorr",
    ],
    (
        "qbittorrent.yml",
        "Remove the retired qbittorrent prefs-check script from the host",
    ): ["/usr/local/bin/qbittorrent-prefs-check.sh", "/opt/qbittorrent-prefs-check"],
    (
        "crowdsec.yml",
        "Remove the retired crowdsec cron scripts from the host",
    ): [
        "/usr/local/bin/crowdsec-update-remote-allowlist.sh",
        "/opt/crowdsec-remote-allowlist",
        "/usr/local/bin/crowdsec-prune-bouncers.sh",
        "/opt/crowdsec-bouncer-prune",
        "/usr/local/bin/crowdsec-appsec-verify.sh",
        "/usr/local/bin/crowdsec-update-home-allowlist.sh",
        "/opt/crowdsec-home-allowlist",
        "/var/lib/crowdsec-home-allowlist",
    ],
}
