"""The CI contexts a promotion requires, read from the checkout, and `promote.py ci-verdict`.

`decide` reduces each candidate's check-runs with the shared `ci_verdict` (`hosts/lib/`) over
the contexts `read_ci_contexts` returns, so a promotion, a GitOps tick and the host packages'
builder agree on what green means. `ci_check` prints that reduction for one commit, for the render job's re-check of a
dispatched `sha`. `promote.py` re-exports all three names.
"""

import sys
from pathlib import Path

import yaml

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
# `forge` imports bare, from scripts/deploy_tools.
_sys.path.insert(1, str(_Path(__file__).resolve().parents[1]))
from lib import yaml_fast
from lib.cli_help import error
from lib.exit_codes import FAILED, OK
from lib.repo_paths import HOST_LIB_FILES

import forge
from deploy_tools.promote_lib.prune import READ_ERRORS, ReadError

# ci_verdict imports nothing first-party, unlike deploy_logic, which re-exports the whole deployer.
sys.path.insert(0, str(HOST_LIB_FILES))
from ci_verdict import ci_verdict

# The required check-run names, as one literal so a `git grep` for the path or the key finds
# this reader. The key moved from the deployer's role defaults to group_vars in H7.2e.
CI_CONTEXTS_SOURCE = "ansible/inventory/group_vars/all.yml"
CI_CONTEXTS_KEY = "ci_required_contexts"


def read_ci_contexts(path: Path) -> list[str]:
    """The `ci_required_contexts` list in the YAML file at `path`.

    Raises:
        ReadError: the file is unreadable, or the key is missing or is anything but a
            non-empty list of non-empty strings. A Jinja reference such as
            `"{{ ci_required_contexts }}"`, which the deployer's role default holds, is a
            string, so it raises too.
    """
    try:
        data = yaml_fast.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as err:
        raise ReadError(f"{path}: {err}") from err
    value = data.get(CI_CONTEXTS_KEY) if isinstance(data, dict) else None
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(name, str) and name.strip() for name in value)
    ):
        raise ReadError(
            f"{CI_CONTEXTS_KEY} in {path} must be a non-empty list of non-empty strings, "
            f"not {value!r}"
        )
    return value


def ci_check(f: forge.Forge, repo: Path, sha: str) -> int:
    """Print one commit's CI verdict, as `decide` reduces it, and return 0 only for `pass`.

    The render job runs it on a dispatched `sha`, which carries no CI conclusion of its own.
    """
    try:
        verdict = ci_verdict(
            f.github.check_runs(sha),
            frozenset(read_ci_contexts(repo / CI_CONTEXTS_SOURCE)),
        )
    except READ_ERRORS as err:
        error(f"ci-verdict {sha}: {err}")
        return FAILED
    print(f"sha={sha} ci={verdict}")
    return OK if verdict == "pass" else FAILED
