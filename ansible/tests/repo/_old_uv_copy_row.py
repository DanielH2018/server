"""The census row that holds every unit and script to `/usr/bin/uv`, kept out of the row table."""

import re

from _row_table import Census, Subject, lines_matching, tracked

NOTHING_READS_THE_OLD_UV_COPY = Census(
    name="nothing-reads-the-old-uv-copy",
    reason=(
        "daniel-server's package retires /usr/local/bin/uv, and daniel-box's retires it in "
        "H7.62, so a unit or script naming that path runs a missing binary there. Name "
        "/usr/bin/uv, which the `uv` component ships on both hosts. The allowed files write "
        "or retire the path. Delete this row with the tasks in H7.62."
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
        {"ansible/roles/setup/gitops_deploy/templates/gitops-deploy.service.j2"}
    ),
    allow={
        "ansible/roles/setup/initial_setup/defaults/main.yml": "names the subject",
        "ansible/roles/setup/initial_setup/tasks/host-basics.yml": "writes it until H7.62",
        "hosts/components/uv/component.yml": "says who retires it",
        "hosts/daniel-box/package.yml": "says who keeps writing it",
        "hosts/daniel-server/package.yml": "the legacy entry",
    },
)
