"""The Renovate packageRules policy as a table: what each named dep resolves to.

Each row resolves one field for one dep and one update type through `_resolve_setting`, which
walks the packageRules in order the way Renovate does, so a later rule that overrides an
earlier one is what the row sees. A row with `rules` resolves against a mutated rule list
instead: those rows are the red halves, each showing that the rule they name, in the place it
sits, is what decides the value. Rows that share a `shares` key must resolve to one value,
which is how a lockstep group is held in one PR.

The reason behind each policy is in that rule's `description` in `renovate.json`. A guard that
needs a census or a join to another file keeps its own `test_renovate_*.py` module.

Run: uv run pytest scripts/tests/test_renovate_package_rules.py
"""

from collections import defaultdict

import pytest
from _renovate import _REPO
from _renovate_rule_rows import (
    CI,
    HOST_ROWS,
    PI_COMPOSE,
    Dep,
    Has,
    Row,
    appended,
    edited,
    k8s_image,
    moved,
    resolve,
    without,
)

# The Pi's Compose images, as the operator ruled on 2026-10-29 (H5 Open question 1).
AUTOHEAL = Dep(
    "willfarrell/autoheal", f"{PI_COMPOSE}/autoheal/compose.yaml.j2", "docker"
)
PROXY = Dep(
    "lscr.io/linuxserver/socket-proxy",
    f"{PI_COMPOSE}/docker-proxy/compose.yaml.j2",
    "docker",
)
WG_EASY = Dep(
    "ghcr.io/wg-easy/wg-easy", f"{PI_COMPOSE}/wg-easy/compose.yaml.j2", "docker"
)
ALLOY_PI = Dep("grafana/alloy", f"{PI_COMPOSE}/alloy/compose.yaml.j2", "docker")
ALLOY_K8S = k8s_image("grafana/alloy", "loki-homelab")
PI_RULES = (
    "One PR per Pi Compose service",
    "Digest bumps to the Pi's Compose images",
    "The Pi's wg-easy pin",
    "Grafana Alloy is pinned twice",
)

# The nginx alpine digests soak 1 day; the rest of the k8s plane's digests soak 3.
NGINX = k8s_image("nginx", "freshrss", tag="alpine")
NGINX_UNPRIVILEGED = k8s_image("nginxinc/nginx-unprivileged", "docs", tag="alpine")
FLARESOLVERR = k8s_image("ghcr.io/flaresolverr/flaresolverr", "prowlarr")
DIGEST_RULE = "Digest bumps on the k8s plane"

# A k8s image update with no release timestamp raises instead of pending forever.
NO_TIMESTAMP_DIGESTS = [
    k8s_image("lscr.io/linuxserver/qbittorrent", "qbittorrent"),
    k8s_image("ghcr.io/schaka/janitorr", "janitorr"),
    k8s_image("favonia/cloudflare-ddns", "cloudflare-ddns"),
]
NO_TIMESTAMP_VERSIONS = [
    k8s_image("lscr.io/linuxserver/bazarr", "bazarr"),
    k8s_image("lscr.io/linuxserver/sonarr", "sonarr"),
    k8s_image("lscr.io/linuxserver/homeassistant", "home-assistant"),
    k8s_image("ghcr.io/raydak-labs/configarr", "configarr"),
    k8s_image("gcr.io/go-containerregistry/crane", "registry"),
]
OPTIONAL = "timestamp-optional"

# The branches that chased master, with the update type each carried.
CHASED = [
    (NGINX_UNPRIVILEGED, "digest"),
    (k8s_image("lscr.io/linuxserver/radarr", "radarr"), "patch"),
    (k8s_image("ghcr.io/headlamp-k8s/headlamp", "headlamp"), "minor"),
]


IMAGE_ROWS = [
    *(
        Row(f"pi-{d.name}-digest-{field}", d, "digest", field, value)
        for d in (AUTOHEAL, PROXY)
        for field, value in (
            ("automerge", True),
            ("groupName", "pi image {{depName}}"),
            ("minimumReleaseAgeBehaviour", OPTIONAL),
            ("rebaseWhen", "conflicted"),
        )
    ),
    *(
        Row(f"pi-wg-easy-{u}-{field}", WG_EASY, u, field, value)
        for u in ("digest", "major")
        for field, value in (("automerge", False), ("groupName", Has("(manual")))
    ),
    *(
        Row(
            f"alloy-{d.file}-{u}-group",
            d,
            u,
            "groupName",
            Has("(manual", "k8s_autodeploy: false"),
            shares="alloy",
        )
        for d in (ALLOY_PI, ALLOY_K8S)
        for u in ("digest", "minor", "major")
    ),
    *(
        Row(f"alloy-pi-{u}-automerge", ALLOY_PI, u, "automerge", False)
        for u in ("digest", "minor", "major")
    ),
    Row(
        "pi-rules-dropped-alloy-automerges",
        ALLOY_PI,
        "minor",
        "automerge",
        True,
        rules=without(*PI_RULES),
    ),
    Row(
        "pi-rules-dropped-alloy-joins-the-catch-all",
        ALLOY_PI,
        "minor",
        "groupName",
        "container images (non-major)",
        rules=without(*PI_RULES),
    ),
    *(
        Row(f"nginx-{d.name}-{u}-soak", d, u, "minimumReleaseAge", "1 day")
        for d in (NGINX, NGINX_UNPRIVILEGED)
        for u in ("digest", "pinDigest")
    ),
    *(
        Row(
            f"nginx-unprivileged-{u}-soak",
            NGINX_UNPRIVILEGED,
            u,
            "minimumReleaseAge",
            "7 days",
        )
        for u in ("minor", "patch")
    ),
    Row(
        "k8s-digest-outside-the-exception-soaks-3-days",
        FLARESOLVERR,
        "digest",
        "minimumReleaseAge",
        "3 days",
    ),
    Row(
        "nginx-exception-hoisted-above-the-digest-rule-soaks-3-days",
        NGINX,
        "digest",
        "minimumReleaseAge",
        "3 days",
        rules=moved("The nginx alpine digests", before=DIGEST_RULE),
    ),
    *(
        Row(f"no-timestamp-{d.name}-{u}", d, u, "minimumReleaseAgeBehaviour", OPTIONAL)
        for d in NO_TIMESTAMP_DIGESTS
        for u in ("digest", "pinDigest")
    ),
    *(
        Row(f"no-timestamp-{d.name}-{u}", d, u, "minimumReleaseAgeBehaviour", OPTIONAL)
        for d in NO_TIMESTAMP_VERSIONS
        for u in ("minor", "patch")
    ),
    Row(
        "no-timestamp-stays-required-outside-the-k8s-plane",
        Dep("vale-cli/vale", CI, "docker"),
        "minor",
        "minimumReleaseAgeBehaviour",
        None,
    ),
    Row(
        "no-timestamp-stays-required-off-the-docker-datasource",
        Dep("ruff", "k8s/roles/code-server/defaults/main.yml", "pypi"),
        "minor",
        "minimumReleaseAgeBehaviour",
        None,
    ),
    Row(
        "digest-rule-without-the-key-requires-a-timestamp",
        NO_TIMESTAMP_DIGESTS[0],
        "digest",
        "minimumReleaseAgeBehaviour",
        None,
        rules=edited(DIGEST_RULE, lambda rule: rule.pop("minimumReleaseAgeBehaviour")),
    ),
    *(
        Row(f"chased-{d.name}-{field}", d, u, field, value)
        for d, u in CHASED
        for field, value in (("automerge", True), ("rebaseWhen", "conflicted"))
    ),
    Row(
        "later-rule-resetting-rebase-when-wins",
        CHASED[1][0],
        CHASED[1][1],
        "rebaseWhen",
        "auto",
        rules=appended({"matchManagers": ["custom.regex"], "rebaseWhen": "auto"}),
    ),
]
ROWS = [*HOST_ROWS, *IMAGE_ROWS]


@pytest.mark.parametrize("row", ROWS, ids=lambda r: r.id)
def test_each_row_resolves_as_the_table_says(row: Row) -> None:
    # fact: .claude/skills/renovate-prs/SKILL.md#0. An update with no PR is still an update
    assert resolve(row) == row.expected


def test_rows_sharing_a_group_resolve_to_one_value() -> None:
    values: dict[str, set] = defaultdict(set)
    for row in ROWS:
        if row.shares:
            values[row.shares].add(resolve(row))
    split = {key: found for key, found in values.items() if len(found) != 1}
    assert not split, f"a lockstep group splits into several PRs: {split}"


def test_row_ids_are_unique() -> None:
    ids = [row.id for row in ROWS]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize(
    "dep",
    sorted({row.dep for row in ROWS if row.dep.tag is not None}),
    ids=lambda d: f"{d.name}@{d.file}",
)
def test_each_image_pin_still_sits_where_the_rule_looks(dep: Dep) -> None:
    """A moved pin leaves its rule matching nothing, and its rows resolving a path no pin has."""
    lines = [
        line
        for line in (_REPO / dep.file).read_text().splitlines()
        if f"_image: {dep.name}:" in line
    ]
    assert lines, (
        f"{dep.file} no longer pins {dep.name}; move the rule and its rows with it"
    )
    tag = lines[0].split(f"_image: {dep.name}:", 1)[1].split("@")[0]
    assert (dep.tag or "") in tag, (
        f"{dep.file} pins {dep.name}:{tag}, not a {dep.tag} tag"
    )
