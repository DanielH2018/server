"""karakeep-chrome runs on a read-only root, so every path it writes needs an emptyDir.

The chrome container carries `readOnlyRootFilesystem: true`, and headless chromium writes to
exactly two paths outside its image tree: `/tmp` (profile, crash dumps, and the shm files
`--disable-dev-shm-usage` moves out of /dev/shm) and `/var/cache/fontconfig`. Neither failure
is loud. A missing /tmp mount is the one that breaks rendering; a missing fontconfig cache only
logs `Fontconfig error: No writable cache directories` and makes chromium rescan the font tree
on every process start, which is why it survived PR #1164 and had to be found in the pod's log
(#1174). Both are invisible to every other guard here: `test_container_security_context.py`
reads the securityContext without asking what the container then needs writable, and
`test_volume_names_descriptive.py` checks a mount resolves to a volume without caring which
mounts exist.

WHAT THIS PINS: that each required path is mounted AND that its volume is an emptyDir. The
second half matters — a PVC at /var/cache/fontconfig would satisfy a mount-path check while
being the wrong thing entirely (RWO, node-pinning a pod that has no state to keep), and
`_writable_mounts` is written so a non-emptyDir volume does not count.
`test_an_unbacked_mount_is_flagged` is that rejection, run against a synthetic doc rather than
the tree, because the tree is supposed to be clean and a rule that stopped matching would
otherwise pass silently.

The container lookup raises rather than returning an empty set: a rename of the Deployment or
the container would make the loop yield nothing, and an assertion over nothing passes.

Run: uv run pytest ansible/tests/services/test_karakeep_chrome_writable_paths.py
"""

import pytest
from _k8s_render import rendered_docs

# The paths chromium writes under a read-only root. Adding one is a tightening; removing one
# needs evidence from the pod, not from the manifest.
REQUIRED_WRITABLE = frozenset({"/tmp", "/var/cache/fontconfig"})


def _writable_mounts(doc: dict, container: str) -> set[str]:
    """Mount paths in `container` that an emptyDir backs — the ones a read-only root can write."""
    spec = doc["spec"]["template"]["spec"]
    empty_dirs = {v["name"] for v in spec.get("volumes", []) if "emptyDir" in v}
    for c in spec["containers"]:
        if c["name"] != container:
            continue
        return {
            m["mountPath"] for m in c.get("volumeMounts", []) if m["name"] in empty_dirs
        }
    raise AssertionError(f"no container named {container!r}")


def _chrome_deployment() -> dict:
    for role, _, doc in rendered_docs():
        if (
            role == "karakeep"
            and doc.get("kind") == "Deployment"
            and doc["metadata"]["name"] == "karakeep-chrome"
        ):
            return doc
    raise AssertionError("karakeep renders no Deployment named karakeep-chrome")


def test_the_chrome_container_is_clean():
    """The accept half, against the tree: read-only root, and every write path given back."""
    doc = _chrome_deployment()
    container = next(
        c
        for c in doc["spec"]["template"]["spec"]["containers"]
        if c["name"] == "chrome"
    )
    assert container["securityContext"]["readOnlyRootFilesystem"] is True, (
        "this guard only means anything while the root is read-only"
    )
    missing = REQUIRED_WRITABLE - _writable_mounts(doc, "chrome")
    assert not missing, f"karakeep-chrome cannot write {sorted(missing)}"


@pytest.mark.parametrize(
    "volume",
    [
        pytest.param(None, id="volume-missing"),
        pytest.param(
            {
                "name": "chrome-fontconfig-cache",
                "persistentVolumeClaim": {"claimName": "karakeep-fontconfig"},
            },
            id="not-an-emptydir",
        ),
    ],
)
def test_an_unbacked_mount_is_flagged(volume):
    """The reject half: a mount whose volume is absent or is not an emptyDir must not count."""
    doc = {
        "spec": {
            "template": {
                "spec": {
                    "volumes": [{"name": "chrome-tmp", "emptyDir": {}}]
                    + ([volume] if volume else []),
                    "containers": [
                        {
                            "name": "chrome",
                            "volumeMounts": [
                                {"name": "chrome-tmp", "mountPath": "/tmp"},
                                {
                                    "name": "chrome-fontconfig-cache",
                                    "mountPath": "/var/cache/fontconfig",
                                },
                            ],
                        }
                    ],
                }
            }
        }
    }
    assert REQUIRED_WRITABLE - _writable_mounts(doc, "chrome") == {
        "/var/cache/fontconfig"
    }
