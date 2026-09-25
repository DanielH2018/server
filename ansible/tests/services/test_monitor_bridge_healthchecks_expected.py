"""monitor-bridge's Healthchecks.io expectations agree with the crons and with the deadman doc.

`monitor_bridge_healthchecks_expected` is what checks/healthchecks.py holds the live console to
(#2566). It is a literal because the cron variables live in three other roles' defaults, which
the deploy play cannot read from monitor-bridge's templates. This guard is what stops the
literal drifting from its sources: each slug's period or cron expression must equal what the
deadman-cadences fragment assembles from the cron variables, and each slug's schedule type and
grace must equal the period-and-grace table in docs/healthchecks-io-deadman.md.

Run: uv run pytest ansible/tests/services/test_monitor_bridge_healthchecks_expected.py
"""

import re

from _helpers import REPO
from fragment_readers import role_defaults
from fragment_renderers import deadman_crons
from gen_doc_fragments import K3S_DEFAULTS, PI_PEER_DEFAULTS, REGISTRY_DEFAULTS

BRIDGE_DEFAULTS = REPO / "ansible/roles/k8s/monitor-bridge/defaults/main.yml"
DOC = REPO / "docs/healthchecks-io-deadman.md"

# The census the comparisons below must cover, so an empty list cannot pass them vacuously.
WIRED_SLUGS = frozenset(
    {
        "longhorn-backup-health",
        "daniel-box-disk-health",
        "uptime-kuma-alive",
        "manifest-prune-check",
        "etcd-snapshot-offbox",
        "pi-peer-backup",
        "registry-gc",
    }
)

_EVERY_N_MINUTES = re.compile(r"^\*/(\d+) \* \* \* \*$")
_GRACE = re.compile(r"^(\d+) (minute|hour)s?$")


def _expected() -> dict[str, dict]:
    rows = role_defaults(BRIDGE_DEFAULTS)["monitor_bridge_healthchecks_expected"]
    return {r["slug"]: r for r in rows}


def _crons() -> dict[str, str]:
    rows = deadman_crons(
        role_defaults(K3S_DEFAULTS),
        role_defaults(PI_PEER_DEFAULTS),
        role_defaults(REGISTRY_DEFAULTS),
    )
    return {slug: cron for slug, cron, _ in rows}


def _doc_table() -> dict[str, tuple[str, int]]:
    """slug -> (schedule type, grace in seconds), from the doc's period-and-grace table."""
    section = DOC.read_text().split("## Period and grace")[1].split("\n## ")[0]
    table: dict[str, tuple[str, int]] = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 3 or not cells[0].startswith("`"):
            continue
        m = _GRACE.match(cells[2])
        assert m, "unparseable grace %r for %s" % (cells[2], cells[0])
        seconds = int(m.group(1)) * (60 if m.group(2) == "minute" else 3600)
        table[cells[0].strip("`")] = (cells[1].lower(), seconds)
    return table


def cron_mismatches(expected: dict[str, dict], crons: dict[str, str]) -> list[str]:
    """Every slug whose expected period or cron expression differs from its cron variable."""
    out = []
    for slug, want in expected.items():
        cron = crons.get(slug)
        if want["kind"] == "simple":
            m = _EVERY_N_MINUTES.match(cron or "")
            if not m or int(m.group(1)) * 60 != want["timeout"]:
                out.append(
                    "%s: Simple %ss against cron %r" % (slug, want["timeout"], cron)
                )
        elif want["schedule"] != cron:
            out.append("%s: Cron %r against cron %r" % (slug, want["schedule"], cron))
    return out


def test_the_expectations_cover_exactly_the_wired_slugs():
    assert set(_expected()) == WIRED_SLUGS
    assert set(_crons()) == WIRED_SLUGS
    assert set(_doc_table()) == WIRED_SLUGS


def test_every_expectation_matches_the_cron_that_pings_it_is_clean():
    assert cron_mismatches(_expected(), _crons()) == []


def test_a_cron_moved_without_the_expectation_is_flagged():
    # The #2563 shape, on the repo side: the CronJob moved to `0 23`, the expectation did not.
    expected = _expected()
    expected["pi-peer-backup"] = dict(
        expected["pi-peer-backup"], schedule="30 23 * * *"
    )
    assert cron_mismatches(expected, _crons()) == [
        "pi-peer-backup: Cron '30 23 * * *' against cron '0 23 * * *'"
    ]


def test_every_expectation_matches_the_docs_schedule_type_and_grace():
    doc = _doc_table()
    for slug, want in _expected().items():
        assert doc[slug] == (want["kind"], want["grace"]), slug


def test_only_pi_peer_backup_runs_in_the_containers_timezone():
    # A k8s CronJob with `timeZone: {{ tz }}`; every other slug is a UTC host cron.
    cronjob = (
        REPO / "ansible/roles/k8s/pi-peer-backup/templates/cronjob.yaml.j2"
    ).read_text()
    assert "timeZone: {{ tz }}" in cronjob
    tzs = {s: w.get("tz") for s, w in _expected().items() if w["kind"] == "cron"}
    assert tzs.pop("pi-peer-backup") == "{{ tz }}"
    assert set(tzs.values()) == {"UTC"}
