"""The census row that holds every unit and script to `/usr/bin/uv`, kept out of the row table."""

import re

from _row_table import Census, Subject, lines_matching, tracked

_BOX = "runs on daniel-box alone until H7.62"

NOTHING_READS_THE_OLD_UV_COPY = Census(
    name="nothing-reads-the-old-uv-copy",
    reason=(
        "daniel-server's package retires /usr/local/bin/uv, so a unit or script it runs that "
        "names the path runs a missing binary. Name /usr/bin/uv, which the `uv` component ships "
        "on both hosts. The allowed files write or retire the path, or run on daniel-box alone, "
        "where the path stays until H7.62. H7.62 repoints them and deletes this row."
    ),
    files=lambda: [
        rel
        for rel in tracked("ansible/roles/*", "hosts/*", "scripts/*")
        if "/tests/" not in rel
        and rel.endswith((".j2", ".yml", ".yaml", ".sh", ".py", ".service", ".timer"))
    ],
    offence=lines_matching(re.compile(r"/usr/local/bin/uv\b")),
    red=(Subject("a.j2", "ExecStart=/usr/local/bin/uv run x\n"),),
    green=(Subject("a.j2", "ExecStart=/usr/bin/uv run x\n"),),
    must_find=frozenset(
        {"ansible/roles/setup/initial_setup/templates/loki-read-route-health.sh.j2"}
    ),
    allow={
        "ansible/roles/setup/initial_setup/defaults/main.yml": "names the subject",
        "ansible/roles/setup/initial_setup/tasks/host-basics.yml": "writes it until H7.62",
        "hosts/components/uv/component.yml": "says who retires it",
        "hosts/daniel-box/package.yml": "says who keeps writing it",
        "hosts/daniel-server/package.yml": "the legacy entry",
        "ansible/roles/k8s/manifests/tasks/release_digest.yml": _BOX,
        "ansible/roles/setup/common/tasks/release_bin.yml": _BOX,
        "ansible/roles/setup/deploy_ui/templates/deploy-ui.service.j2": _BOX,
        "ansible/roles/setup/fake_remux/tasks/main.yml": _BOX,
        "ansible/roles/setup/fake_remux/templates/fake-remux-health.sh.j2": _BOX,
        "ansible/roles/setup/gitops_deploy/templates/gitops-deploy.service.j2": _BOX,
        "ansible/roles/setup/k3s/templates/longhorn-backup-health.sh.j2": _BOX,
        "ansible/roles/setup/renovate_agent/templates/renovate-agent-land@.service.j2": _BOX,
        "ansible/roles/setup/renovate_notify/templates/renovate-notify.service.j2": _BOX,
        "scripts/backup/etcd_restore_drill.sh": _BOX,
    },
)
