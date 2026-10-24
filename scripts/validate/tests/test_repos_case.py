"""The `homelab-repos` container case's verdict, without a container (H7.24a).

The case itself runs only in CI's `host_packages` job. This holds what it decides from the
container's output, with a green and a red input for each thing it reads, and holds that the job
runs it.

Run: uv run pytest scripts/validate/tests/test_repos_case.py
"""

import hashlib
from pathlib import Path

from validate import host_package_cases
from validate.validate_lib.repos_case import PACKAGE, takeover_problems

PATHS = {
    "/etc/apt/keyrings/docker.asc": hashlib.sha256(b"key").hexdigest(),
    "/etc/apt/sources.list.d/docker.sources": hashlib.sha256(b"source").hexdigest(),
}
OWNERS = "".join(f"{PACKAGE}: {path}\n" for path in PATHS)
DIGESTS = "".join(f"{digest}  {path}\n" for path, digest in PATHS.items())
CONTROL = "./\n./control\n./md5sums\n"


def problems(**changes) -> list[str]:
    args = {
        "expected": PATHS,
        "owners": OWNERS,
        "verify": "",
        "control": CONTROL,
        "digests": DIGESTS,
        **changes,
    }
    return takeover_problems(**args)


def test_a_takeover_that_owns_every_path_with_its_bytes_has_no_problem():
    assert problems() == []


def test_a_path_the_package_does_not_own_is_flagged():
    owners = f"{PACKAGE}: /etc/apt/keyrings/docker.asc\n"
    assert problems(owners=owners) == [
        f"dpkg -S does not name {PACKAGE} for /etc/apt/sources.list.d/docker.sources"
    ]


def test_a_file_dpkg_left_as_it_found_it_is_flagged():
    stale = DIGESTS.replace(PATHS["/etc/apt/keyrings/docker.asc"], "0" * 64)
    (problem,) = problems(digests=stale)
    assert problem.startswith("/etc/apt/keyrings/docker.asc holds 000000000000")


def test_a_verify_line_or_a_conffile_is_flagged():
    assert problems(verify="??5??????  /etc/apt/keyrings/docker.asc\n")
    assert problems(control=CONTROL + "./conffiles\n")


def test_the_host_packages_job_runs_the_case():
    assert "repos_case.run(cases)" in Path(host_package_cases.__file__).read_text()
