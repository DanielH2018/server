"""The values the renderer derives from the tree that no role file states.

`content_tag` is the name a built image is pushed under, and every consumer's image ref reads
it. A moved tag rebuilds all nine images and rolls their pods.
"""

import hashlib
import json


def content_tag(data: dict[str, str]) -> str:
    """The content tag of a build whose context ConfigMap holds `data`, as `sha-<12 hex>`.

    The hash covers `data` alone, so a comment in the context template moves no tag. Keys sort
    case-insensitively, so `app.py` sorts before `Dockerfile`, and `deploy_tools.ci_images`
    computes the same value without importing the renderer.
    """
    pairs = [[key, data[key]] for key in sorted(data, key=str.lower)]
    return "sha-" + hashlib.sha256(json.dumps(pairs).encode()).hexdigest()[:12]
