"""configarr's TRaSH/recyclarr clone cache is an `emptyDir`, never a PVC.

The cache is two public git repos re-cloned in seconds. On a persistent volume it outlives
the pod, and a clone that stopped updating — a moved default branch, a rebased history the
fetch refuses — keeps serving last month's guides while the sync reports green; the failure
only shows when a CF the guide dropped is still being applied. Ephemeral storage makes every
nightly run start from upstream. `test_cronjob_only_roles_include_the_gate.py` covers the
job's gate; this covers the one volume whose type changes what the job proves.

Run: uv run pytest ansible/tests/services/test_configarr_repos_cache_is_ephemeral.py
"""

from _k8s_render import rendered_docs

CACHE_VOLUME = "configarr-repos"


def cache_volume(cronjob: dict) -> dict:
    volumes = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["volumes"]
    matches = [v for v in volumes if v.get("name") == CACHE_VOLUME]
    assert len(matches) == 1, (
        f"expected one `{CACHE_VOLUME}` volume, found {len(matches)}"
    )
    return matches[0]


def _configarr_cronjob() -> dict:
    jobs = [
        doc
        for role, _tpl, doc in rendered_docs()
        if role == "configarr" and doc.get("kind") == "CronJob"
    ]
    assert len(jobs) == 1, "configarr renders exactly one CronJob"
    return jobs[0]


def test_the_repos_cache_is_an_empty_dir():
    volume = cache_volume(_configarr_cronjob())
    assert "emptyDir" in volume and "persistentVolumeClaim" not in volume, volume


def test_a_claim_backed_cache_is_flagged():
    cronjob = {
        "spec": {
            "jobTemplate": {
                "spec": {
                    "template": {
                        "spec": {
                            "volumes": [
                                {
                                    "name": CACHE_VOLUME,
                                    "persistentVolumeClaim": {
                                        "claimName": "configarr-repos"
                                    },
                                }
                            ]
                        }
                    }
                }
            }
        }
    }
    assert "emptyDir" not in cache_volume(cronjob)
