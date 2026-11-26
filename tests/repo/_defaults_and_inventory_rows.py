"""Five census rows over role defaults, the inventory and the Longhorn queries in templates.

`test_census_rows_roles.py` runs them with its own rows, from a module of their own so that
file stays under its module-length cap.
"""

import re
from pathlib import PurePosixPath

from _row_table import Census, Subject, text_lacks, tracked
from lib import yaml_fast
from renderer.renderer_lib.restart import DEFAULT_TRIGGERS

_ROLE_DEFAULTS = "k8s/roles/*/defaults/main.yml"

# ── Every external image pin carries a digest ───────────────────────────────────────────

# Mirrors renovate.json's k8s-defaults `matchStrings` entry: repo, tag, optional digest, so a
# pin one of them can see is a pin the other can see too.
_IMAGE = re.compile(
    r"^\s*(?P<var>[a-z0-9_]*_image):\s*[\"']?"
    r"(?P<repo>[^:\s\"'@]+):(?P<tag>[^\s\"'@]+)"
    r"(?:@(?P<digest>sha256:[a-f0-9]+))?[\"']?"
)


def _image_pins(subject: Subject) -> list[re.Match[str]]:
    return [
        m
        for line in subject.text.splitlines()
        if not line.lstrip().startswith("#") and (m := _IMAGE.match(line))
    ]


_DIGEST = "@sha256:" + "a" * 64

_IMAGE_PINS_CARRY_A_DIGEST = Census(
    name="every-external-image-pin-carries-a-digest",
    reason=(
        "A tag is mutable: the publisher can re-push it, so the bytes a run validates are not "
        "the bytes a later run pulls, which weakens a `--dry-run`, a rollback digest and an "
        "image a promotion records as pinned. Base images (`alpine:3.24`, "
        "`python:3.14-alpine`) name a patch stream and are re-pushed routinely. Renovate adds "
        "digests itself (`pinDigests` on the k8s-defaults manager), so a bare pin is one it "
        "could not see or was told to skip, and one it skips carries a digest moved by hand. "
        "The tag stays beside the digest (`repo:tag@sha256:...`): the manager captures "
        "`currentDigest` as an optional group after `currentValue`, so a bare `repo@sha256:` "
        "freezes the pin. The selector is the files that manager reads, so a pin this row "
        "covers is one Renovate can bump. A built image starts with "
        "`{{ k8s_registry_pull_host }}`, which the pattern cannot match."
    ),
    files=lambda: tracked(_ROLE_DEFAULTS, "k8s/vars.yml"),
    offence=lambda s: [
        f"{m.group('var')}: {m.group('repo')}:{m.group('tag')} carries no digest"
        for m in _image_pins(s)
        if not m.group("digest")
    ],
    red=(
        Subject("a.yml", "seed_volume_image: alpine:3.24"),
        Subject("a.yml", "monitor_bridge_k8s_image: python:3.14-alpine"),
        Subject("a.yml", "karakeep_k8s_chrome_image: zenika/alpine-chrome:124  # note"),
        Subject("a.yml", '  docs_k8s_image: "nginxinc/nginx-unprivileged:1.29-alpine"'),
        Subject("a.yml", "authelia_k8s_image: authelia/authelia:4.39.20"),
    ),
    green=(
        Subject("a.yml", f"seed_volume_image: alpine:3.24{_DIGEST}"),
        Subject(
            "a.yml",
            f"karakeep_k8s_chrome_image: zenika/alpine-chrome:124{_DIGEST}  # ok",
        ),
        Subject("a.yml", f"authelia_k8s_image: authelia/authelia:4.39.20{_DIGEST}"),
        Subject("a.yml", "# seed_volume_image: alpine:3.24"),
        Subject(
            "a.yml",
            'code_server_k8s_image: "{{ k8s_registry_pull_host }}/code-server:latest"',
        ),
    ),
    count=lambda s: len(_image_pins(s)),
    min_matches=21,
    must_find=frozenset({"k8s/vars.yml", "k8s/roles/traefik/defaults/main.yml"}),
)

# ── Each restart_on entry names only signals the renderer knows ─────────────────────────


def _narrowed_entries(subject: Subject) -> list[tuple[str, list[str]]]:
    """(workload name, restart_on) for each rollout entry in the defaults that narrows."""
    defaults = yaml_fast.safe_load(subject.text) or {}
    return [
        (entry["name"], entry["restart_on"])
        for value in defaults.values()
        if isinstance(value, list)
        for entry in value
        if isinstance(entry, dict) and "name" in entry and "restart_on" in entry
    ]


_RESTART_ON_NAMES_KNOWN_SIGNALS = Census(
    name="restart-on-names-only-known-signals",
    reason=(
        "`restart_on` narrows which change signal stamps a restart on one rollout entry. A "
        "name that matches no signal in `renderer_lib/restart.py:DEFAULT_TRIGGERS` is never "
        "restarted, and nothing reports it."
    ),
    files=lambda: tracked(_ROLE_DEFAULTS),
    offence=lambda s: [
        f"{name} declares restart_on {sorted(unknown)}, which the renderer does not know "
        f"({sorted(DEFAULT_TRIGGERS)})"
        for name, triggers in _narrowed_entries(s)
        if (unknown := set(triggers) - DEFAULT_TRIGGERS)
    ],
    red=(
        Subject(
            "a.yml",
            "rollouts:\n  - name: web\n    restart_on: [configmap, no-such-signal]\n",
        ),
    ),
    green=(
        Subject(
            "a.yml",
            f"rollouts:\n  - name: web\n    restart_on: [{min(DEFAULT_TRIGGERS)}]\n",
        ),
        Subject("a.yml", "rollouts:\n  - name: web\n"),
    ),
    # A defaults file that narrows an entry is a member, so a renamed key fails the floor.
    count=lambda s: len(_narrowed_entries(s)),
    min_matches=3,
    must_find=frozenset(
        f"k8s/roles/{role}/defaults/main.yml"
        for role in ("observability", "karakeep", "flux")
    ),
)

# ── Observability's expected netpol exempt set is empty ─────────────────────────────────

_OBS_EXEMPT_EMPTY = "netpol_baseline_obs_exempt_workloads: []"

_OBSERVABILITY_EXEMPT_SET_IS_EMPTY = Census(
    name="netpol-baseline-observability-exempt-set-is-empty",
    reason=(
        "An exemption in observability's expected set leaves a pod fenced by nothing, or by "
        "only its homelab-caller grant, which cuts prometheus's, loki's and alertmanager's "
        "in-namespace callers (the defaults file's comment on the variable has why). The "
        "live gate is the `exempt` check of the `netpol-baseline-verify` Job, which "
        "`k8s/roles/netpol-baseline/tests/test_netpol_baseline_flux_gates.py` pins."
    ),
    files=lambda: tracked("k8s/roles/netpol-baseline/defaults/main.yml"),
    offence=text_lacks(_OBS_EXEMPT_EMPTY, "sets observability's exempt set to []"),
    red=(Subject("a.yml", "netpol_baseline_obs_exempt_workloads: [prometheus]\n"),),
    green=(Subject("a.yml", f"{_OBS_EXEMPT_EMPTY}\n"),),
)

# ── No inventory block scalar carries a `#`-led continuation line ───────────────────────

# The indicator starts a block; the indentation of the line that carries it ends one.
_BLOCK_START = re.compile(r"^(\s*)(?:[^#\s][^#]*?:|-)\s*[|>][0-9+-]*\s*(?:#.*)?$")


def _comment_shaped_continuations(subject: Subject) -> list[str]:
    """Each block-scalar continuation line whose first character is `#`, by line.

    A blank line inside a block does not end it; a non-blank line indented no deeper than
    the line carrying the indicator does.
    """
    hits = []
    block_indent = None
    for n, line in enumerate(subject.text.splitlines(), 1):
        if block_indent is not None:
            if not line.strip():
                continue
            if len(line) - len(line.lstrip()) > block_indent:
                if line.lstrip().startswith("#"):
                    hits.append(
                        f"line {n}: a `#` line inside a block scalar is content"
                    )
                continue
            block_indent = None
        if m := _BLOCK_START.match(line):
            block_indent = len(m.group(1))
    return hits


_INVENTORY_BLOCK_SCALARS_HAVE_NO_COMMENT_SHAPED_LINE = Census(
    name="inventory-block-scalars-have-no-comment-shaped-line",
    reason=(
        "`role_reads._defines_only` decides whether an inventory change only defines new keys "
        "by skipping every whole-line comment. Inside a block scalar (`key: |` or `key: >`) a "
        "continuation line starting with `#` is content Ansible reads, so a key consumed only "
        "there would be skipped in the unsafe direction: the narrowing deploys fewer services "
        "than the change reaches. `_defines_only` relies on no such line existing. Move the "
        "text out of the block scalar, or teach `_defines_only` to parse. A `_`-prefixed file "
        "is out of scope: no host loads it, and `_sort_hits` exempts it on both sides."
    ),
    files=lambda: [
        rel
        for rel in tracked(
            "estate/vars.yml", "k8s/vars.yml", "hosts/vars.yml", "hosts/*/vars.yml"
        )
        if not any(part.startswith("_") for part in PurePosixPath(rel).parts)
    ],
    offence=_comment_shaped_continuations,
    red=(
        Subject(
            "a.yml",
            "pihole_lists: |\n  https://example.invalid/a\n"
            "  # not a comment: Ansible hands this line on\n  https://example.invalid/b\n",
        ),
        *(Subject("a.yml", f"k: {m}\n  # x\n") for m in ("|", "|-", ">", ">+", "|2")),
        Subject("a.yml", "items:\n  - |\n    # x\n"),
    ),
    green=(
        Subject(
            "a.yml",
            "ssh_keys: |\n  ssh-ed25519 AAAA one\n# a real comment after the block\n"
            "banner: >-\n  hello\n\n  world\nafter: 1\n",
        ),
        Subject(
            "a.yml", "k: |\n  content\n# comment\n  # deeper, but the block ended\n"
        ),
    ),
    must_find=frozenset(
        {
            "estate/vars.yml",
            "k8s/vars.yml",
            "hosts/vars.yml",
            "hosts/daniel-box/vars.yml",
            "hosts/daniel-server/vars.yml",
            "hosts/daniel-pi/vars.yml",
        }
    ),
)

# ── Only a Backup CR query reads `.status.volumeName` ───────────────────────────────────

# `kubectl get <plural>.longhorn.io`, capturing which resource the pipeline reads.
_LONGHORN_GET = re.compile(r"\bget\s+([a-z]+)\.longhorn\.io\b")
_READS_VOLUMENAME = re.compile(r"\.status\.volumeName\b")


def _code_lines(subject: Subject) -> list[tuple[int, str]]:
    return [
        (n, line)
        for n, line in enumerate(subject.text.splitlines(), 1)
        if not line.lstrip().startswith("#")
    ]


def _volume_cr_volumename_reads(subject: Subject) -> list[str]:
    """Each `.status.volumeName` read under a non-Backup Longhorn `get`, by line.

    A read with no preceding `get ...longhorn.io` is not flagged: the row cannot attribute
    it, and guessing would fire on unrelated code.
    """
    hits = []
    resource = None
    for n, line in _code_lines(subject):
        if found := _LONGHORN_GET.search(line):
            resource = found.group(1)
        if _READS_VOLUMENAME.search(line) and resource not in (None, "backups"):
            hits.append(f"line {n}: .status.volumeName read off {resource}.longhorn.io")
    return hits


_ONLY_A_BACKUP_QUERY_READS_VOLUMENAME = Census(
    name="only-a-backup-query-reads-status-volumename",
    reason=(
        "Longhorn puts `.status.volumeName` on Backup CRs, not Volume CRs. Read off a Volume "
        "it yields null, which compares equal to nothing, so the filter silently matches an "
        "empty set (`longhorn-restore-drill.sh.j2` records the night it did). The nearest "
        "preceding `get <resource>.longhorn.io` names the resource a pipeline reads. Comment "
        "lines are skipped, because the prose explaining this trap mentions the field. Use "
        "`.metadata.name` for a Volume."
    ),
    files=lambda: tracked("k8s/*.j2", "k8s/*.yml", "inventory/*.j2", "inventory/*.yml"),
    offence=_volume_cr_volumename_reads,
    red=(
        Subject(
            "a.sh.j2",
            "VOLS=$($KUBECTL get volumes.longhorn.io -o json \\\n"
            "  | jq '[.items[] | select(.status.volumeName == $v)]')\n",
        ),
        Subject(
            "a.sh.j2",
            "A=$($KUBECTL get backups.longhorn.io -o json | jq '.items[].status.volumeName')\n"
            "B=$($KUBECTL get volumes.longhorn.io -o json | jq '.items[].status.volumeName')\n",
        ),
    ),
    green=(
        Subject(
            "a.sh.j2",
            "BACKED=$($KUBECTL get backups.longhorn.io -o json \\\n"
            "  | jq '[.items[] | .status.volumeName]')\n",
        ),
        Subject(
            "a.sh.j2",
            "VOLS=$($KUBECTL get volumes.longhorn.io -o json \\\n"
            "  # .metadata.name, NOT .status.volumeName\n"
            "  | jq '[.items[] | .metadata.name]')\n",
        ),
        Subject("a.sh.j2", 'echo "{{ item.status.volumeName }}"\n'),
    ),
    # A file holding a Longhorn `get` is a member: the row reads what those pipelines read.
    count=lambda s: sum(1 for _, line in _code_lines(s) if _LONGHORN_GET.search(line)),
    must_find=frozenset(
        {"k8s/roles/longhorn-restore-drill/templates/longhorn-restore-drill.sh.j2"}
    ),
)

DEFAULTS_AND_INVENTORY_ROWS = (
    _IMAGE_PINS_CARRY_A_DIGEST,
    _RESTART_ON_NAMES_KNOWN_SIGNALS,
    _OBSERVABILITY_EXEMPT_SET_IS_EMPTY,
    _INVENTORY_BLOCK_SCALARS_HAVE_NO_COMMENT_SHAPED_LINE,
    _ONLY_A_BACKUP_QUERY_READS_VOLUMENAME,
)
