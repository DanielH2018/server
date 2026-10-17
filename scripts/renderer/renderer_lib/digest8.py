"""8-hex-digit digests of a Job's spec and a ConfigMap's content, shared by snapshot and soak."""

import hashlib
import json
from collections.abc import Mapping


def job_spec_digest8(job: Mapping) -> str:
    """The first 8 hex digits of the SHA-256 of a Job's `spec`, as sorted-key compact JSON.

    Every Job Flux applies ends its name in `-<this>` (D5, *Rules for every Job Flux
    applies*). A Job's pod template is immutable, so a spec change must also change the name,
    and the name is left out of what is hashed. The Job census in
    `scripts/renderer/tests/test_renderer_job_census.py` checks each name against it, so the
    first renderer kind that names a Job (D5.2a's snapshot) names it with this too.
    """
    text = json.dumps(job.get("spec"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def configmap_digest8(configmap: Mapping) -> str:
    """The first 8 hex digits of the SHA-256 of a ConfigMap's `data` and `binaryData`.

    A Job's script ConfigMap ends its name in `-<this>`, and the Job's spec names the
    ConfigMap, so `job_spec_digest8` covers the script and a script fix renames the Job (D5,
    *Script delivery*).
    """
    content = {k: configmap.get(k) or {} for k in ("data", "binaryData")}
    text = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:8]
