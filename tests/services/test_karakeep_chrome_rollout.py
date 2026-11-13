"""karakeep-chrome is listed as a rollout and takes no restart stamp from a karakeep edit (#4564).

`karakeep_k8s_chrome_image` moves with every Renovate bump, which rolls chrome's pod template.
meilisearch and time-tagger read env from the role's Secret, so a manifest change restarts them.
chrome reads nothing the role renders, so `restart_on: []` keeps an unrelated karakeep edit from
dropping a crawl in flight.

Run: uv run pytest tests/services/test_karakeep_chrome_rollout.py
"""

from _helpers import K8S_ROLES
from renderer.renderer_lib.task_inputs import include_vars

KARAKEEP = K8S_ROLES / "karakeep"
CHROME = "karakeep-chrome"


def _extras() -> dict[str, dict]:
    return {
        entry["name"]: entry
        for entry in include_vars(KARAKEEP)["manifests_extra_rollouts"]
    }


def test_chrome_restarts_on_nothing():
    assert _extras()[CHROME]["restart_on"] == []


def test_the_sidecars_keep_the_shared_restart():
    extras = _extras()
    for sidecar in ("karakeep-meilisearch", "karakeep-time-tagger"):
        assert extras[sidecar].get("restart_on") != []
