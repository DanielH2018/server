# ansible/roles/setup/gitops_deploy/files/deploy_release.py
"""Reading the k8s release and render records this deployer did not write.

`roles/k8s/manifests/tasks/release_stamp.yml` records, per service, the commit that produced
its applied manifests — after EVERY real apply, including one an operator ran by hand. That
last part is the whole reason this module exists: an operator's `./scripts/deploy.sh` is
invisible to the deployer, and the record is the only evidence of it this host holds.
`deploy_k8s_owed.discharge_k8s_unapplied` is the reader, and its docstring carries the design.

The render records (`roles/k8s/manifests/tasks/render_record.yml`) come from the hourly
`render_records` producer. `render_proof` lets a render stand in for a deploy of a shared role
(#3057), and `digest_diff` and `applied_diff` feed the deploy-plane shadow log (#3045).

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
    including one an operator ran by hand — which is why `deploy_k8s_owed.discharge_k8s_unapplied`
    reads it. None for a record that is absent, unreadable, unparseable or missing the field;
    that docstring says why every caller treats None as "no evidence of a deploy".
    """
    try:
        record = json.loads(pathlib.Path(release_dir, f"{service}.json").read_text())
    except OSError, ValueError:
        return None
    commit = record.get("commit") if isinstance(record, dict) else None
    return commit or None


# Mirrors `manifests_render_record_dir` in the same defaults file, for the reason
# `K8S_RELEASE_DIR` does. `tests/test_k8s_unapplied_marker.py` asserts the two agree.
K8S_RENDER_DIR = "/var/lib/homelab/k8s-renders.d"

# The three answers `digest_verdict` gives, spelled as `probe_lib/releases_render.py` spells
# them. `tests/test_deploy_release_digest.py` runs both functions over the same records.
CURRENT = "current"
DRIFTED = "drifted"
UNKNOWN = "unknown"


def _record(service: str, directory: str) -> dict | None:
    """`<directory>/<service>.json` as a dict, or None for anything unreadable."""
    try:
        record = json.loads(pathlib.Path(directory, f"{service}.json").read_text())
    # Split clauses for the reason `release_commit` gives.
    except OSError:
        return None
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _incomparable(release: dict | None, render: dict | None, ref: str) -> str:
    """Why the two records' digests cannot be compared at `ref`, or '' when they can.

    A stdlib restatement of `probe_lib/releases_render.py:_comparable`, which this unit
    cannot import. That module's docstring carries the reasoning behind every refusal. The
    reason string exists for the shadow log, which counts why a service had no answer.
    """
    if not release:
        return "no release record"
    if not render:
        return "no render record"
    if render.get("commit") != ref:
        return "render is of another commit"
    if render.get("tree_dirty") is not False:
        return "render tree was dirty"
    if not release.get("host") or release.get("host") != render.get("host"):
        return "host differs"
    if not release.get("manifests_digest") or not render.get("manifests_digest"):
        return "no manifests digest"
    names, render_names = (
        release.get("secret_manifests"),
        render.get("secret_manifests"),
    )
    if names is None or render_names is None:
        return "no secret manifest list"
    if (names or render_names) and not (
        release.get("secret_digest") and render.get("secret_digest")
    ):
        return "no secret digest"
    return ""


def digest_verdict(
    release: dict | None, render: dict | None, ref: str
) -> tuple[str, str]:
    """`(CURRENT | DRIFTED | UNKNOWN, why unknown)` for one service's two records at `ref`."""
    why = _incomparable(release, render, ref)
    if why or release is None or render is None:
        return UNKNOWN, why
    names = release.get("secret_manifests")
    if (
        release["manifests_digest"] != render["manifests_digest"]
        or names != render.get("secret_manifests")
        or (names and release.get("secret_digest") != render.get("secret_digest"))
    ):
        return DRIFTED, ""
    return CURRENT, ""


def render_proof(
    service: str,
    release_dir: str = K8S_RELEASE_DIR,
    render_dir: str = K8S_RENDER_DIR,
) -> str | None:
    """The commit whose render matches `service`'s applied bytes, or None.

    A render at commit C whose digests equal the release record's proves the applied
    manifests ARE what C renders, whenever the apply happened. The probe reader asks that only
    at the tip of origin/master; this asks it at the render's own commit, and the caller asks
    whether C descends from the change. The hourly producer's commit descends from a merge
    within about an hour, where "the render is of the tip" holds for minutes a day (#3057).
    """
    render = _record(service, render_dir)
    commit = render.get("commit") if render else None
    if not commit:
        return None
    release = _record(service, release_dir)
    return commit if digest_verdict(release, render, commit)[0] == CURRENT else None


def digest_diff(
    ref: str, release_dir: str = K8S_RELEASE_DIR, render_dir: str = K8S_RENDER_DIR
) -> dict[str, list[str]]:
    """`digest_verdict` at `ref` for every service holding either record, grouped.

    Keys are `CURRENT`, `DRIFTED`, and `UNKNOWN: <why>` for each refusal seen; each value is
    a sorted service list. The deploy-plane shadow log reads this (#3045).
    """
    names = {
        path.stem
        for directory in (release_dir, render_dir)
        for path in pathlib.Path(directory).glob("*.json")
        if not path.name.endswith(".previous.json")
    }
    out: dict[str, list[str]] = {}
    for service in sorted(names):
        verdict, why = digest_verdict(
            _record(service, release_dir), _record(service, render_dir), ref
        )
        out.setdefault(f"{verdict}: {why}" if why else verdict, []).append(service)
    return out


# The three answers `applied_diff` gives for one service.
MOVED = "moved"
UNCHANGED = "unchanged"
UNSTAMPED = "unstamped"


def release_records(release_dir: str = K8S_RELEASE_DIR) -> dict[str, dict]:
    """Every service's release record, keyed by service, for a before/after comparison."""
    out = {}
    for path in pathlib.Path(release_dir).glob("*.json"):
        if path.name.endswith(".previous.json"):
            continue
        record = _record(path.stem, release_dir)
        if record is not None:
            out[path.stem] = record
    return out


def _digests(record: dict | None) -> tuple | None:
    if record is None:
        return None
    return (
        record.get("manifests_digest"),
        record.get("secret_manifests"),
        record.get("secret_digest"),
    )


def applied_diff(
    before: dict[str, dict], after: dict[str, dict], ref: str
) -> dict[str, list[str]]:
    """Which services an apply of `ref` changed, read from the release records either side.

    `release_stamp.yml` hashes an applied service through the same `release_digest.yml` a
    render record uses, so a record stamped at `ref` is as good as a render of `ref`. That
    makes a full play its own render, at no extra cost, for every service it stamped (#3045).
    MOVED is what a digest diff at `ref` would have applied: digests that differ from the
    record before, or no record before. UNSTAMPED is a service with no usable record at `ref`
    afterwards — one the play did not reach, or whose record `_incomparable` would refuse.
    Each value is a sorted service list; a key with no services is absent.
    """
    out: dict[str, list[str]] = {}
    for service in sorted(set(before) | set(after)):
        record = after.get(service)
        if record is None or _incomparable(record, record, ref):
            verdict = UNSTAMPED
        elif _digests(record) == _digests(before.get(service)):
            verdict = UNCHANGED
        else:
            verdict = MOVED
        out.setdefault(verdict, []).append(service)
    return out
