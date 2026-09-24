#!/usr/bin/env python3
"""The k8s karakeep role vendors the time-tagger script; the Docker role downloaded it.

Those were two different mechanisms for getting the same bytes into the same program, and the
whole reason the Docker role was safe is its pinned commit plus sha256 — a guarantee a vendored
copy does not inherit. This guard holds the vendored copy to that pin.

The vendored copy exists because a ConfigMap renders from a file that must be present at render
time, and CI renders every manifest template on a machine that has never run a deploy, so a
lookup on a download destination could only ever fail there.

The Docker role was archived on 2026-08-09 (karakeep runs only in k8s now), and #2385 deleted
the archive. Its get_url task was the sole record of the pinned commit URL + sha256 the vendored
bytes were taken from, so `PINNED_URL` and `PINNED_SHA256` below carry that record. To read the
original, run `git show 2460d0675fd748e70fcbcde87185371ffd62402b:ansible/roles/containers/archive/karakeep/tasks/main.yml`.
To update the vendored script, re-download it from a new commit and change all three together.

Run: uv run pytest ansible/tests/services/test_karakeep_time_tagger_script.py
"""

import hashlib
import re
from _helpers import ANSIBLE


VENDORED = ANSIBLE / "roles" / "k8s" / "karakeep" / "files" / "karakeep-time-tagger.py"
PINNED_URL = (
    "https://raw.githubusercontent.com/thiswillbeyourgithub/karakeep_python_api/"
    "37fa33fdd62b2ee5275ce8cdc1c86d29d0ec4237/community_scripts/karakeep-time-tagger/"
    "karakeep-time-tagger.py"
)
PINNED_SHA256 = "066c675df6f316cd4419d376ee5f63b5f3ab871216b40d76f5f4545d6cc74dd1"


def test_the_vendored_script_matches_the_pinned_checksum():
    actual = hashlib.sha256(VENDORED.read_bytes()).hexdigest()

    assert actual == PINNED_SHA256, (
        f"{VENDORED.name} hashes {actual} but the pin records {PINNED_SHA256}. The vendored "
        "copy has drifted: re-download from PINNED_URL, or update the URL, checksum and file "
        "together."
    )


def test_the_pinned_url_names_an_immutable_commit():
    """A branch or tag ref would make the checksum a snapshot of whatever upstream happened to
    be serving, so a legitimate re-download could silently change what runs."""
    assert re.search(
        r"/raw\.githubusercontent\.com/[^/]+/[^/]+/[0-9a-f]{40}/", PINNED_URL
    ), f"{PINNED_URL} does not pin a full commit SHA"
