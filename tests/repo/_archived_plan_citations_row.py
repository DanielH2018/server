"""The census row holding shipped files' plan citations to `docs/archive/`."""

import re

from _row_table import Census, Subject, lines_matching, tracked

SHIPPED_FILES_CITE_NO_TARGET_ARCHITECTURE_STUB = Census(
    name="shipped-files-cite-no-target-architecture-stub",
    reason=(
        "A migration plan lives under `docs/archive/`, and `docs/target-architecture/` holds "
        "only the docs-guard slices. A `#` line in a role or a host package renders into a "
        "manifest or a package, so a citation of a stub path there cannot be repointed "
        "without restarting the workload. Cite the archived path from the start."
    ),
    files=lambda: tracked("k8s/*", "hosts/*", "inventory/*"),
    offence=lines_matching(re.compile(r"docs/target-architecture/(?!docs-guard/)")),
    red=(
        Subject(
            "a.yaml",
            "# (docs/target-architecture/monitoring/m5-loki-ruler.md) says so\n",
        ),
    ),
    green=(
        Subject("a.yaml", "# (docs/archive/monitoring/m5-loki-ruler.md) says so\n"),
        Subject("a.sh", "# docs/target-architecture/docs-guard/handoff.md\n"),
    ),
    min_matches=1000,
    must_find=frozenset({"k8s/roles/loki-homelab/files/rules/homelab-logs.yaml"}),
)
