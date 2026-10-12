"""karakeep-chrome is waited on by the batch drain and never restarted by the role (#4564).

`karakeep_k8s_chrome_image` moves with every Renovate bump. A bump changes chrome's pod
template, so the apply rolls it. Before #4564 chrome sat in no rollout list, so nothing waited
on that roll, and the landing of #4551 printed `VERDICT: settled` while the new pod was still
pulling. karakeep now lists chrome in `manifests_extra_rollouts`, which queues it for the
drain, with `restart_on: []`, which keeps the shared extra restart from rolling it on an
unrelated karakeep edit and dropping a crawl in flight.

Rendered through Ansible's own filters against karakeep's real include vars. The harness's
`default` filter cannot see a key an entry omits (see the docstring of
`k8s/test_release_stamp_rollout_expectation.py`), so each entry gets the `kind` and `image`
the play's `| default(...)` would give it.

Run: uv run pytest ansible/tests/services/test_karakeep_chrome_rollout.py
"""

from _helpers import ANSIBLE
from _release_expectation import (
    extra_restarts,
    include_role_vars,
    queued_extras,
    rollouts,
)

KARAKEEP = ANSIBLE / "roles/k8s/karakeep"
CHROME = "karakeep-chrome"

# A ConfigMap edit to the app: every manifest signal fires, and no pod template moves.
_UNRELATED_EDIT = dict(
    manifests_service="karakeep",
    manifests_rollout="karakeep",
    manifests_apply={"changed": True, "stdout": "configmap/karakeep-env configured"},
)


def _extras() -> list[dict]:
    return [
        {"kind": "deploy", "image": entry["name"], **entry}
        for entry in include_role_vars(KARAKEEP)["manifests_extra_rollouts"]
    ]


def test_a_chrome_image_bump_queues_chrome_for_the_drain():
    """The verify-by of #4564: the apply rolled chrome, so the drain must wait on it and the
    stabilisation gate must soak it."""
    queued = queued_extras(
        _extras(),
        manifests_apply={
            "changed": True,
            "stdout": f"deployment.apps/{CHROME} configured",
        },
    )
    chrome = [r for r in queued if r["name"] == CHROME]
    assert chrome, (
        f"{CHROME} is not queued for the drain, so a chrome bump lands unwaited"
    )
    assert chrome[0]["changed"] is True, (
        "the stabilisation gate would skip the rolled pod"
    )


def test_an_unrelated_karakeep_edit_restarts_the_sidecars_but_not_chrome():
    """meilisearch and time-tagger read env from the role's Secret, so the role restarts them
    on every manifest change. chrome reads nothing the role renders, so it must not."""
    restarted = {
        item["name"]: extra_restarts(item, **_UNRELATED_EDIT) for item in _extras()
    }
    assert restarted == {
        "karakeep-meilisearch": True,
        "karakeep-time-tagger": True,
        CHROME: False,
    }
    record = rollouts(manifests_extra_rollouts=_extras(), **_UNRELATED_EDIT)
    assert record[CHROME]["restart"] is False, (
        "the release record expects a restart of chrome that the role never issues, so "
        "probe.py health reads it as NOT ROLLED"
    )
    assert record["karakeep-meilisearch"]["restart"] is True
