#!/usr/bin/env python3
"""The k8s karakeep role vendors the time-tagger script; the Docker role downloads it.

Those are two different mechanisms for getting the same bytes into the same program, and the
whole reason the Docker role is safe is its pinned commit plus sha256 — a guarantee a vendored
copy does not inherit. Nothing else would notice the two drifting apart: both roles deploy
cleanly, and the taggers just start behaving differently on hosts nobody is comparing.

The vendored copy exists because a ConfigMap renders from a file that must be present at render
time, and CI renders every manifest template on a machine that has never run a deploy, so a
lookup on a download destination could only ever fail there.

Run: uv run pytest ansible/tests/test_karakeep_time_tagger_script.py
"""

import hashlib
import re
from pathlib import Path

ANSIBLE = Path(__file__).resolve().parents[1]
VENDORED = ANSIBLE / "roles" / "k8s" / "karakeep" / "files" / "karakeep-time-tagger.py"
DOCKER_TASKS = ANSIBLE / "roles" / "containers" / "karakeep" / "tasks" / "main.yml"


def _docker_role_pin() -> tuple[str, str]:
    """The (url, sha256) the Docker role downloads and verifies."""
    text = DOCKER_TASKS.read_text()
    url = re.search(r'url:\s*"(\S+karakeep-time-tagger\.py)"', text)
    checksum = re.search(r'checksum:\s*"sha256:([0-9a-f]{64})"', text)
    assert url and checksum, (
        "could not parse the time-tagger get_url from the Docker role — if that task moved or "
        "was rewritten, this guard is no longer watching anything"
    )
    return url.group(1), checksum.group(1)


def test_the_vendored_script_matches_the_checksum_the_docker_role_verifies():
    _, expected = _docker_role_pin()

    actual = hashlib.sha256(VENDORED.read_bytes()).hexdigest()

    assert actual == expected, (
        f"{VENDORED.name} hashes {actual} but the Docker role verifies {expected}. The two "
        "copies have drifted: re-download from the pinned URL, or update both together."
    )


def test_the_pinned_url_names_an_immutable_commit():
    """A branch or tag ref would make the checksum a snapshot of whatever upstream happened to
    be serving, so a legitimate re-download could silently change what runs."""
    url, _ = _docker_role_pin()

    assert re.search(r"/raw\.githubusercontent\.com/[^/]+/[^/]+/[0-9a-f]{40}/", url), (
        f"{url} does not pin a full commit SHA"
    )
