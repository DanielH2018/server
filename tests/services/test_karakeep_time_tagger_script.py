#!/usr/bin/env python3
"""The karakeep role carries a fork of the upstream time-tagger script, held to a sha256.

The script is upstream's file at `PINNED_URL` with the logging changes of #8985 (one filtered
INFO handler, no file sink, no non-TTY progress bar). A fork has no upstream bytes to match, so
this guard holds the copy to `FORK_SHA256` and keeps `PINNED_URL` as the commit it forked from.

The script is vendored because a ConfigMap renders from a file that must be present at render
time, and CI renders every manifest template on a machine that has never run a deploy, so a
lookup on a download destination could only ever fail there.

To change the script, edit it and update `FORK_SHA256` together; to rebase onto a newer
upstream, re-apply the logging changes to the new commit and update `PINNED_URL` as well.

Run: uv run pytest tests/services/test_karakeep_time_tagger_script.py
"""

import hashlib
import re
from _helpers import K8S_ROLES


VENDORED = K8S_ROLES / "karakeep" / "files" / "karakeep-time-tagger.py"
PINNED_URL = (
    "https://raw.githubusercontent.com/thiswillbeyourgithub/karakeep_python_api/"
    "37fa33fdd62b2ee5275ce8cdc1c86d29d0ec4237/community_scripts/karakeep-time-tagger/"
    "karakeep-time-tagger.py"
)
FORK_SHA256 = "f6baff066cb6af85b655593389bf7ba84e3cfd8a9c22d62fd2739a8d6ee240ec"


def test_the_vendored_script_matches_the_pinned_checksum():
    actual = hashlib.sha256(VENDORED.read_bytes()).hexdigest()

    assert actual == FORK_SHA256, (
        f"{VENDORED.name} hashes {actual} but the test records {FORK_SHA256}. Update "
        "FORK_SHA256 in the same change as an intended edit to the script."
    )


def test_the_pinned_url_names_an_immutable_commit():
    """A branch or tag ref would make the checksum a snapshot of whatever upstream happened to
    be serving, so a legitimate re-download could silently change what runs."""
    assert re.search(
        r"/raw\.githubusercontent\.com/[^/]+/[^/]+/[0-9a-f]{40}/", PINNED_URL
    ), f"{PINNED_URL} does not pin a full commit SHA"
