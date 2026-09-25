# ansible/roles/setup/gitops_deploy/files/deploy_release.py
"""Reading the k8s release records this deployer did not write.

`roles/k8s/manifests/tasks/release_stamp.yml` records, per service, the commit that produced
its applied manifests — after EVERY real apply, including one an operator ran by hand. That
last part is the whole reason this module exists: an operator's `./scripts/deploy.sh` is
invisible to the deployer, and the record is the only evidence of it this host holds.
`deploy_defer.discharge_k8s_unapplied` is the reader, and its docstring carries the design.

Its own module rather than a section of `deploy_io.py`, which is at its length ceiling and
whose allowlist entry only ever falls.

Stdlib only, for the reason `deploy_io.py` gives: the unit runs under `uv run --no-project`.
"""

import json
import pathlib

# Mirrors `manifests_release_dir` in roles/k8s/manifests/defaults/main.yml, the way
# `probe_lib/releases.py:RELEASE_DIR` does and for the same reason: this unit runs under
# `uv run --no-project` and cannot import that reader. A drifted path would make every pending
# `k8s_unapplied` line undischargeable in silence, so
# `tests/test_k8s_unapplied_marker.py::test_the_release_dir_matches_the_manifests_role`
# asserts the two agree rather than trusting this comment.
K8S_RELEASE_DIR = "/var/lib/homelab/k8s-releases.d"


def release_commit(service: str, release_dir: str = K8S_RELEASE_DIR) -> str | None:
    """The commit that produced `service`'s applied manifests, or None.

    `roles/k8s/manifests/tasks/release_stamp.yml` writes the record after every real apply,
    including one an operator ran by hand — which is why `deploy_defer.discharge_k8s_unapplied`
    reads it. None for a record that is absent, unreadable, unparseable or missing the field;
    that docstring says why every caller treats None as "no evidence of a deploy".
    """
    try:
        record = json.loads(pathlib.Path(release_dir, f"{service}.json").read_text())
    # Split clauses, not `except (A, B)`: ruff's 3.14 target rewrites a parenthesized tuple
    # into the 3.14-only `except A, B:`, which this unit's host Python 3.12 cannot parse. The
    # same workaround as `deploy_alerts.read_pending`, and it goes when the host moves.
    except OSError:
        return None
    except ValueError:
        return None
    commit = record.get("commit") if isinstance(record, dict) else None
    return commit or None
