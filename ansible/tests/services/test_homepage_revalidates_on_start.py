#!/usr/bin/env python3
"""homepage's pod must re-render `/` from its own config at startup.

The image ships a build-time render of `/` that carries no settings at all. Verified against
the pinned digest (`ghcr.io/gethomepage/homepage:v1.13.2`):
`/app/.next/server/pages/en.json` reads `"initialSettings":{}`, `en.html` reads
`<title data-next-head="">Homepage</title>`, and `prerender-manifest.json` records
`"initialRevalidateSeconds": false`. gethomepage's `getStaticProps` has no `revalidate` key,
so `next build` bakes that page in and nothing expires it.

Upstream's only re-render trigger is client-side — a browser whose stored `/api/hash` value
MISMATCHES the pod's calls `/api/revalidate`; a browser with no stored value stores it and
triggers nothing. A fresh pod visited only by fresh browsers therefore serves the config-less
page for the container's whole life, at 1/1 and with `probe.py health homepage` exiting 0.
The `lifecycle.postStart` hook this guard pins is what removes that state.

Paired per the repo's red-proof rule (`..._is_flagged` / `..._is_clean` over synthetic
manifests) and non-vacuous: the third test asserts the real render still yields a Deployment
named `homepage`, so a role reshuffle that empties the census fails here rather than passing
an `all()` over nothing.

Run: uv run pytest ansible/tests/services/test_homepage_revalidates_on_start.py
"""

import sys as _sys

from _helpers import ANSIBLE as _ANSIBLE

_sys.path.insert(0, str(_ANSIBLE / "tests"))

from _k8s_render import rendered_docs

REVALIDATE_PATH = "/api/revalidate"


def lacks_startup_revalidation(doc):
    """Reason the Deployment does not re-render `/` at startup, or None if it does.

    Reads the `homepage` container's `lifecycle.postStart.exec.command` and requires that it
    names `/api/revalidate` and can never exit non-zero — a failing postStart kills the
    container, turning a cosmetic defect into a crashloop.
    """
    containers = doc["spec"]["template"]["spec"].get("containers", [])
    for container in containers:
        if container.get("name") != "homepage":
            continue
        command = (
            container.get("lifecycle", {})
            .get("postStart", {})
            .get("exec", {})
            .get("command")
        )
        if not command:
            return "no lifecycle.postStart hook on the homepage container"
        text = " ".join(command)
        if REVALIDATE_PATH not in text:
            return f"postStart hook does not call {REVALIDATE_PATH}"
        if "exit 0" not in text:
            return "postStart hook can exit non-zero, which would kill the container"
        return None
    return "no container named homepage"


def _deployment(command):
    return {
        "kind": "Deployment",
        "metadata": {"name": "homepage"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "homepage",
                            **(
                                {
                                    "lifecycle": {
                                        "postStart": {"exec": {"command": command}}
                                    }
                                }
                                if command is not None
                                else {}
                            ),
                        }
                    ]
                }
            }
        },
    }


GOOD_COMMAND = [
    "sh",
    "-c",
    'i=0; while [ "$i" -lt 60 ]; do wget -q -O /dev/null '
    '"http://127.0.0.1:3000/api/revalidate" && break; i=$((i+1)); sleep 1; done; exit 0',
]


def test_a_deployment_without_the_hook_is_flagged():
    assert lacks_startup_revalidation(_deployment(None)) is not None
    assert (
        lacks_startup_revalidation(_deployment(["sh", "-c", "true; exit 0"]))
        is not None
    )
    # Present but able to fail — a non-zero postStart kills the container.
    assert (
        lacks_startup_revalidation(
            _deployment(
                [
                    "sh",
                    "-c",
                    "wget -q -O /dev/null http://127.0.0.1:3000/api/revalidate",
                ]
            )
        )
        is not None
    )


def test_a_deployment_with_the_hook_is_clean():
    assert lacks_startup_revalidation(_deployment(GOOD_COMMAND)) is None


def test_the_rendered_homepage_deployment_revalidates_on_start():
    found = [
        doc
        for role, _tpl, doc in rendered_docs()
        if role == "homepage"
        and doc.get("kind") == "Deployment"
        and doc["metadata"]["name"] == "homepage"
    ]
    # Non-vacuity: without this the loop below passes on an empty render.
    assert len(found) == 1, (
        "the homepage role no longer renders a Deployment named homepage"
    )
    reason = lacks_startup_revalidation(found[0])
    assert reason is None, reason
