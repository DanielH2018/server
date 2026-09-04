"""Fakes shared by the three `probe.py health` suites.

`test_probe_health.py` (docker + rollout + role health), `test_probe_health_resolver.py` and
`test_probe_health_cronjobs.py` all build the same two documents: a fixed `now` and a pods
document carrying container restart counts. They live here rather than in a conftest because
`scripts/conftest.py` is this tree's conftest and covers every suite under `scripts/`, while
these three names are wanted by exactly three modules — the same split
`scripts/deploy_tools/tests/_land_fakes.py` makes.

Import by bare name: pytest puts a test's own directory on `sys.path`, so
`from _probe_health_fixtures import NOW, pods` resolves from any module in this directory.
"""

from datetime import datetime, timezone

# The instant every fake timestamp below is measured against. Fixed rather than
# `datetime.now()` so a restart "120s ago" stays 120s ago on a slow CI runner.
NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)


def pods(*containers):
    """A `kubectl get pods -o json` document for one pod.

    Args:
        *containers: `(name, restart_count, finished_at_or_None)` per container, where
            `finished_at` is the RFC3339 string the last termination carries.
    """
    return {
        "items": [
            {
                "metadata": {"name": "svc-abc"},
                "status": {
                    "containerStatuses": [
                        {
                            "name": name,
                            "restartCount": count,
                            "lastState": (
                                {"terminated": {"finishedAt": finished}}
                                if finished
                                else {}
                            ),
                        }
                        for name, count, finished in containers
                    ]
                },
            }
        ]
    }
