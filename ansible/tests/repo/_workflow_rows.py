"""The census rows over the GitHub Actions workflows.

`test_census_rows_text.py` runs them with its own rows. They sit in a module of their own
because that file is at its module-length cap.
"""

import re

from _row_table import Census, Subject, lines_matching, tracked

_RUNS_ON = re.compile(r"^\s*runs-on:\s*(.+?)\s*$", re.MULTILINE)
_VERSIONED = re.compile(r"^[a-z]+-\d+(\.\d+)?(-[a-z]+)?$")


WORKFLOW_ROWS = (
    Census(
        name="workflow-runners-are-pinned",
        reason=(
            "`runs-on: ubuntu-latest` floats: GitHub rolls the alias with no commit here, so a "
            "`language: system` prek hook can change verdict between two runs of one SHA. A "
            "versioned label (`ubuntu-24.04`) moves only through a Renovate PR, which runs the "
            "hooks on the new image before merge (docs/adr/0018)."
        ),
        files=lambda: tracked(".github/workflows/*.yml"),
        offence=lambda s: [
            f"floating runner {label!r}"
            for label in _RUNS_ON.findall(s.text)
            if not _VERSIONED.match(label)
        ],
        red=(
            Subject("a.yml", "jobs:\n  a:\n    runs-on: ubuntu-latest\n"),
            Subject("b.yml", "jobs:\n  b:\n    runs-on: ${{ env.RUNNER }}\n"),
            Subject("c.yml", "jobs:\n  c:\n    runs-on: self-hosted\n"),
        ),
        green=(Subject("a.yml", "jobs:\n  a:\n    runs-on: ubuntu-24.04\n"),),
        must_find=frozenset(
            f".github/workflows/{name}"
            for name in ("ci.yml", "image-smoke.yml", "renovate-config-canary.yml")
        ),
    ),
    Census(
        name="workflows-fetch-through-the-shared-script",
        reason=(
            "The merge-base fetch needs a token in a one-shot `extraheader`, because every "
            "checkout runs with persist-credentials: false. `scripts/dev/pr_changed_files.sh` "
            "holds that fetch once, and image-smoke and the deck_mod job call it even though "
            "they read none of its file lists (#3777). A workflow that inlines the header again "
            "is a copy the next change to the fetch has to find by hand."
        ),
        files=lambda: tracked(".github/workflows/*.yml"),
        offence=lines_matching(re.compile(r"extraheader")),
        red=(
            Subject(
                "a.yml",
                'run: |\n  git -c "http.https://github.com/.extraheader=AUTHORIZATION: x" '
                "fetch origin main\n",
            ),
        ),
        green=(
            Subject(
                "a.yml",
                'run: |\n  "$GITHUB_WORKSPACE/scripts/dev/pr_changed_files.sh"\n',
            ),
        ),
        must_find=frozenset(
            f".github/workflows/{name}" for name in ("ci.yml", "image-smoke.yml")
        ),
    ),
    Census(
        name="workflow-downloads-retry-every-error",
        reason=(
            "`curl --retry` alone retries only 408, 429 and 5xx, so a 403 from GitHub's "
            "release CDN fails the step on its first answer. That 403 turned master red in "
            "run 40160070856 with no change to the code. `--retry-all-errors` retries it, and "
            "the sha256 check after each download still refuses a wrong file. `-L` is the "
            "download shape: promote's registry probes use `-fsS` and read the status code "
            "themselves, so a 404 there is an answer and must not be retried."
        ),
        files=lambda: tracked(
            ".github/workflows/*.yml", ".github/actions/*/action.yml"
        ),
        offence=lines_matching(
            re.compile(r"\bcurl\b(?!.*--retry-all-errors).*\s-fsSL")
        ),
        red=(
            Subject("a.yml", 'run: curl --retry 3 -fsSLo "$RUNNER_TEMP/bin/sops" \\\n'),
            Subject("b.yml", "run: curl -fsSL -o /tmp/vale.tar.gz \\\n"),
        ),
        green=(
            Subject(
                "a.yml",
                "run: curl --retry 5 --retry-all-errors --retry-delay 5 -fsSLo x \\\n",
            ),
            Subject("b.yml", "  | curl --retry 3 -fsS -K - -o out -w '%{http_code}'\n"),
        ),
        must_find=frozenset(
            {".github/workflows/ci.yml", ".github/workflows/promote.yml"}
        ),
    ),
)
