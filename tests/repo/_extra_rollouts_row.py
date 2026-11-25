"""The census row holding which extra rollouts take no restart stamp from a manifest change."""

from _row_table import Census, Subject, tracked
from lib import yaml_fast

QUIET = {
    "scrutiny": {"scrutiny-influxdb", "scrutiny-collector"},
    "karakeep": {"karakeep-chrome"},
}
STAMPED = {"karakeep": {"karakeep-meilisearch", "karakeep-time-tagger"}}


def _defaults(role: str) -> str:
    return f"k8s/roles/{role}/defaults/main.yml"


def _offence(subject: Subject) -> list[str]:
    role = subject.rel.split("/")[2]
    declared = {
        entry["name"]: entry.get("restart_on")
        for entry in (yaml_fast.safe_load(subject.text) or {}).get(
            "manifests_extra_rollouts"
        )
        or ()
    }
    found = [
        f"{name} is not a listed extra rollout"
        for name in sorted(QUIET.get(role, set()) | STAMPED.get(role, set()))
        if name not in declared
    ]
    found += [
        f"{name} takes the restart stamp: its restart_on is {declared[name]!r}, not []"
        for name in sorted(QUIET.get(role, ()))
        if name in declared and declared[name] != []
    ]
    found += [
        f"{name} restarts on nothing, but reads the role's Secret at start"
        for name in sorted(STAMPED.get(role, ()))
        if declared.get(name) == []
    ]
    return found


EXTRA_ROLLOUTS_RESTART_AS_DECLARED = Census(
    name="extra-rollouts-restart-as-declared",
    reason=(
        "A manifest change stamps every extra rollout with the role's restart checksum unless "
        "its entry says `restart_on: []`, which the renderer honours. scrutiny's collector "
        "re-reads a kubelet-synced ConfigMap on each cron run and its InfluxDB reads its "
        "credentials only at first-run setup, so a restart would only take InfluxDB down. "
        "karakeep-chrome reads nothing the role renders, so a restart drops a crawl in flight "
        "for an unrelated edit. karakeep's meilisearch and time-tagger read env from the role's "
        "Secret, so they keep the shared restart."
    ),
    files=lambda: tracked(*map(_defaults, sorted({*QUIET, *STAMPED}))),
    offence=_offence,
    red=(
        Subject(
            _defaults("scrutiny"),
            "manifests_extra_rollouts:\n  - name: scrutiny-influxdb\n"
            "  - name: scrutiny-collector\n    restart_on: []\n",
        ),
        Subject(_defaults("scrutiny"), "manifests_extra_rollouts: []\n"),
        Subject(
            _defaults("karakeep"),
            "manifests_extra_rollouts:\n  - name: karakeep-meilisearch\n"
            "  - name: karakeep-time-tagger\n    restart_on: []\n"
            "  - name: karakeep-chrome\n    restart_on: []\n",
        ),
        Subject(
            _defaults("karakeep"),
            "manifests_extra_rollouts:\n  - name: karakeep-meilisearch\n"
            "  - name: karakeep-time-tagger\n  - name: karakeep-chrome\n",
        ),
    ),
    green=(
        Subject(
            _defaults("scrutiny"),
            "manifests_extra_rollouts:\n  - name: scrutiny-influxdb\n    restart_on: []\n"
            "  - name: scrutiny-collector\n    restart_on: []\n",
        ),
        Subject(
            _defaults("karakeep"),
            "manifests_extra_rollouts:\n  - name: karakeep-meilisearch\n"
            "    restart_on: ['secret']\n  - name: karakeep-time-tagger\n"
            "  - name: karakeep-chrome\n    restart_on: []\n",
        ),
    ),
    min_matches=2,
    must_find=frozenset(map(_defaults, ("scrutiny", "karakeep"))),
)
