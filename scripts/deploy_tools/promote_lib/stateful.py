"""D5's stateful hold: the no-reset rule in `decide`, and the post `promote.py hold` sends.

THE NO-RESET RULE. In CI, `promote` cannot reach Longhorn, so resetting `prod` after a failed
revision R would apply the old image onto data R may have migrated. So when R's changed tags
include a snapshot role, a failed verification writes DEPLOY_HOLD as `<R> stateful=<names>`
and leaves `prod` on R. `<names>` are the expected names that failed or timed out, and
`unverified` when `verify` ended with no usable verdict. The value never takes C2's `failed=`
form, so the revert rule never passes it, and only the operator clears it. `settle` writes it
and tags R's digest `held`, as H1.1b's forward-only hold does, and wins over that hold when
both apply. The plan is docs/target-architecture/deploy/d5-bulk-move.md, *The stateful hold,
its post, and the reset input (D5.2b1, D5.2b2)*; contracts.md C2 states it as *A hold without
a reset*.

  - A tag CHANGES when its `own` digest in `digests.json` differs from `last-good`'s, or one
    side lacks it. `own` covers `cluster/<tag>/` alone, and it is the digest that renames the
    snapshot Job, so a tag whose `own` is unchanged took no snapshot for R. A bundle with no
    readable `digests.json` counts as every tag changed.
  - A tag is a SNAPSHOT ROLE when R's bundle holds `cluster/<tag>-snapshot/job.yaml`, the Job
    D5.2a renders. Its `digest8` and claims come from that Job's `SNAPSHOT_DIGEST8` and
    `SNAPSHOT_CLAIMS`, so the post names exactly the snapshots the Job took.
  - The TRIGGER is a changed snapshot role, not a failed one. A revision that changes a
    snapshot role and breaks traefik holds without a reset too, because a reset would roll the
    snapshot role back onto data R may have migrated (D5 Open question 1, case a).

THE RECORD. `decide` computes it from R's bundle and `last-good`'s before `prod` moves, and
writes it under `--bundle-dir` as STATEFUL_FILE beside the bundles. It holds every changed tag,
each changed snapshot role's `digest8` and claims, every snapshot role R renders, the soak Job's
name when R renders one, and `sha-<R>`'s manifest digest, which `decide` read from GHCR.

THE POST. `promote.py hold` runs in its own job after `settle`, once `settle` has written the
stateful hold. It posts to the deployer's Discord channel:
  - the held SHA, the hold's value, and that `prod` was not reset;
  - every changed tag and every Kustomization that failed or timed out, each marked as a
    snapshot role or not, so the operator can pick the runbook's branch;
  - when `soak` failed, the soak Job's name and the read-only command that prints the
    services it named;
  - for each changed snapshot role, its `digest8`, each claim's snapshot prefix, and a command
    block in Branch 1's order: classify, two suspends, the `volume-revert.yml` run, two
    resumes. Beside the classify command it prints `prod@sha256:<digest>`, the string a role
    that attempted R reads in `.status.lastAttemptedRevision`. CI cannot read the cluster, so
    there is a block for EVERY changed snapshot role, and the classify step picks which to
    revert (D5 Open question 1, case b).
Discord refuses a message over `forge.DISCORD_MAX` characters, so the post is a header message
and then one message per changed snapshot role, or two when its prefixes and its block
together pass the limit, never cut inside a block. Any failed send
exits 1, so GitHub shows a red run.

Like `promote.py`, this module reaches the network only through `forge.py`
(scripts/deploy_tools/tests/test_promote_forge_boundary.py).

Run: uv run python scripts/deploy_tools/promote.py hold <sha> --hold '<sha> stateful=<names>'
     --bundle-dir <dir> --verdict failed --failed <names>
"""

import argparse
import json
import re
from collections.abc import Callable, Iterable
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
# `forge` and `changed_set` import bare, from scripts/deploy_tools.
_sys.path.insert(1, str(_Path(__file__).resolve().parents[1]))
from lib import yaml_fast
from lib.cli_help import error
from lib.exit_codes import FAILED, OK, USAGE_ERROR

import forge
from changed_set import own_digests
from deploy_tools.promote_lib.prune import ReadError
from renderer.renderer_lib.ordering import split_kind

STATEFUL_FILE = "stateful.json"
# The environment variable the `post` job sets from the repository secret of the same name.
WEBHOOK_ENV = "DEPLOY_DISCORD_WEBHOOK"
SOAK = "soak"
RUNBOOK = "docs/volume-snapshot-drills.md"
_SNAPSHOT_JOB = re.compile(r"cluster/([a-z0-9][a-z0-9-]*)-snapshot/job\.yaml")
_SOAK_DIR = f"cluster/{SOAK}/"
_SHA = re.compile(r"[0-9a-f]{40}")
_NAMES = re.compile(r"[a-z0-9-]+(?:,[a-z0-9-]+)*")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


# -- what `decide` computes ------------------------------------------------------------------


def _jobs(raw: bytes) -> list[dict]:
    return [
        doc
        for doc in yaml_fast.safe_load_all(raw)
        if isinstance(doc, dict) and doc.get("kind") == "Job"
    ]


def snapshot_roles(bundle: dict[str, bytes]) -> dict[str, dict]:
    """Each snapshot role in `bundle`, mapped to `{"digest8": ..., "claims": [...]}`.

    Each claim is `{"name": ..., "namespace": ...}`, read from the Job's `SNAPSHOT_CLAIMS`.

    Raises:
        ReadError: a snapshot directory's `job.yaml` holds no Job whose env carries the tag,
            an 8-hex `SNAPSHOT_DIGEST8` and a JSON list of claims. A half-read role would
            print a block that reverts the wrong snapshots, so `decide` stops instead.
    """
    roles = {}
    for name in sorted(bundle):
        match = _SNAPSHOT_JOB.fullmatch(name)
        if match is None:
            continue
        tag = match.group(1)
        try:
            jobs = _jobs(bundle[name])
            container = jobs[0]["spec"]["template"]["spec"]["containers"][0]
            env = {e["name"]: e["value"] for e in container["env"] if "value" in e}
            claims = json.loads(env["SNAPSHOT_CLAIMS"])
            digest8 = env["SNAPSHOT_DIGEST8"]
        except (IndexError, KeyError, TypeError, ValueError) as err:
            raise ReadError(f"{name}: the snapshot Job is unreadable: {err!r}") from err
        if (
            env.get("SNAPSHOT_TAG") != tag
            or not re.fullmatch(r"[0-9a-f]{8}", str(digest8))
            or not isinstance(claims, list)
            or not all(
                isinstance(c, dict)
                and isinstance(c.get("name"), str)
                and isinstance(c.get("namespace"), str)
                for c in claims
            )
        ):
            raise ReadError(
                f"{name}: the snapshot Job does not carry {tag}'s tag, an 8-hex digest8 "
                "and a list of claims"
            )
        roles[tag] = {
            "digest8": digest8,
            "claims": [
                {"name": c["name"], "namespace": c["namespace"]} for c in claims
            ],
        }
    return roles


def soak_job(bundle: dict[str, bytes]) -> str:
    """The name of the soak Job R's bundle renders under `cluster/soak/`, or "" for none."""
    for name in sorted(bundle):
        if name.startswith(_SOAK_DIR) and name.endswith(".yaml"):
            for job in _jobs(bundle[name]):
                found = str((job.get("metadata") or {}).get("name", ""))
                if found.startswith(f"{SOAK}-"):
                    return found
    return ""


def changed_tags(target: object, base: object | None) -> tuple[list[str], str]:
    """The names whose `own` digest differs between two parsed `digests.json`, and why all.

    Returns `(names, widened)`. `widened` is "" when the digests decided, else why every
    name in R counts as changed: R or `last-good` has no readable `digests.json`, or there is
    no `last-good` yet.
    """
    ours, theirs = own_digests(target), own_digests(base)
    ours.pop("_unclassified", None)
    theirs.pop("_unclassified", None)
    if not ours or base is None or not theirs:
        why = "R has no readable digests.json" if not ours else "last-good has none"
        return sorted(ours), why
    names = ours.keys() | theirs.keys()
    return sorted(n for n in names if ours.get(n) != theirs.get(n)), ""


def record(
    target: dict[str, bytes],
    target_digests: object,
    base_digests: object | None,
    digest: str,
) -> dict:
    """The stateful record for R: what the post prints, and `stateful`, the roles that trigger.

    Args:
        target: R's bundle files.
        target_digests: R's parsed `digests.json`.
        base_digests: `last-good`'s parsed `digests.json`, or None when there is none.
        digest: `sha-<R>`'s manifest digest, as `decide` read it from GHCR.

    Raises:
        ReadError: a snapshot Job in R's bundle is unreadable (`snapshot_roles`).
    """
    roles = snapshot_roles(target)
    changed, widened = changed_tags(target_digests, base_digests)
    if widened:
        # Unknown changes count every snapshot role as changed: the side that never resets.
        changed = sorted(set(changed) | set(roles))
    stateful = sorted(set(changed) & set(roles))
    return {
        "digest": digest,
        "changed": changed,
        "widened": widened,
        "snapshot_roles": sorted(roles),
        "stateful": {tag: roles[tag] for tag in stateful},
        "soak_job": soak_job(target),
    }


def write(directory: Path, found: dict) -> None:
    """Write the record under `directory` as STATEFUL_FILE.

    Raises:
        OSError: the write failed.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / STATEFUL_FILE).write_text(
        json.dumps(found, sort_keys=True) + "\n", encoding="utf-8"
    )


def read(directory: Path) -> dict:
    """The record `decide` wrote under `directory`.

    Raises:
        ValueError: the file is missing, not JSON, or lacks a field the post prints. The post
            then fails red rather than posting blocks it cannot fill.
    """
    try:
        found = json.loads((directory / STATEFUL_FILE).read_text(encoding="utf-8"))
    except OSError as err:
        raise ValueError(f"{directory / STATEFUL_FILE}: {err}") from err
    fields = ("digest", "changed", "snapshot_roles", "stateful", "soak_job")
    if not isinstance(found, dict) or any(f not in found for f in fields):
        raise ValueError(f"{directory / STATEFUL_FILE} lacks one of {fields}")
    if not _DIGEST.fullmatch(str(found["digest"])):
        raise ValueError(f"{STATEFUL_FILE}: digest {found['digest']!r} is not sha256")
    if not found["stateful"]:
        raise ValueError(f"{STATEFUL_FILE} names no changed snapshot role")
    return found


# -- the post --------------------------------------------------------------------------------


def _patch(name: str, suspend: bool) -> str:
    body = json.dumps({"spec": {"suspend": suspend}}, separators=(",", ":"))
    return (
        f"sudo k3s kubectl -n flux-system patch kustomization {name} --type merge "
        f"--field-manager=homelab-operator -p '{body}'"
    )


def prefix(tag: str, digest8: str, claim: str) -> str:
    """A claim's snapshot prefix, by the rule `renderer_lib/snapshot.py:snapshot_name` uses."""
    short = claim.removeprefix(f"{tag}-") or claim
    return f"autodeploy-{tag}-{digest8}-{short}-"


def comparison(digest: str) -> str:
    """The string `.status.lastAttemptedRevision` reads on a role that attempted R."""
    return f"prod@{digest}"


def block(tag: str, role: dict, digest: str) -> str:
    """One changed snapshot role's commands, in Branch 1's order, as a fenced block."""
    claims = json.dumps([c["name"] for c in role["claims"]])
    lines = [
        "```",
        f"# 1. classify: {tag} attempted R only when this prints {comparison(digest)}",
        f"kubectl -n flux-system get kustomization {tag} -o "
        "jsonpath='{.status.lastAttemptedRevision}'",
        "# 2. suspend, before any revert",
        _patch(f"{tag}-snapshot", True),
        _patch(tag, True),
        "# 3. revert the volumes",
        f"uv run ansible-playbook ansible/volume-revert.yml -e volume_revert_service={tag} "
        f"""-e '{{"volume_revert_claims": {claims}}}' -e volume_revert_sha={role["digest8"]}""",
        "# 4. resume, only after the reset_to_last_good dispatch and its OCIRepository wait",
        _patch(f"{tag}-snapshot", False),
        _patch(tag, False),
        "```",
    ]
    return "\n".join(lines)


def _mark(name: str, snapshot: Iterable[str]) -> str:
    tag = split_kind(name)[0]
    if tag in snapshot:
        return (
            f"{name} (snapshot role {tag})"
            if tag != name
            else f"{name} (snapshot role)"
        )
    return f"{name} (not a snapshot role)"


def messages(
    target: str, hold: str, verdict: str, failed: list[str], found: dict
) -> list[str]:
    """The post: a header message, then one message per changed snapshot role.

    A role whose prefixes and block together pass `forge.DISCORD_MAX` takes two messages.

    Args:
        target: R, the held commit.
        hold: the DEPLOY_HOLD value `settle` wrote.
        verdict: `verify`'s verdict, `failed` or anything else for no verdict.
        failed: the names `verify` printed as failed or timed out.
        found: the record `decide` wrote (`read`).
    """
    snapshot = set(found["snapshot_roles"])
    stateful = found["stateful"]
    marker = f"promote hold {target[:8]}:"
    changed = ", ".join(_mark(n, snapshot) for n in found["changed"]) or "none"
    if found.get("widened"):
        changed += f" (every name counts as changed: {found['widened']})"
    if verdict == "failed" and failed:
        failing = ", ".join(_mark(n, snapshot) for n in failed)
    else:
        failing = "none named, because verify ended with no verdict"
    header = [
        f"{marker} DEPLOY_HOLD set to `{hold}`. prod was NOT reset and stays on "
        f"sha-{target}, because it changes the snapshot role(s) {', '.join(sorted(stateful))}.",
        f"Changed tags: {changed}",
        f"Failed or timed out: {failing}",
    ]
    if SOAK in failed:
        job = found.get("soak_job") or ""
        if job:
            header += [
                f"soak failed. Its Job is {job}; this prints the services it named:",
                f"```\nkubectl -n flux-system get pods -l job-name={job} -o "
                "jsonpath='{.items[*].status.containerStatuses[0].state.terminated.message}'"
                "\n```",
            ]
        else:
            header.append("soak failed, and R's bundle renders no soak Job to read.")
    header.append(
        f"Pick the branch in {RUNBOOK} from these reads, before any reset. Branch 1 runs "
        "each block below whose classify step prints the comparison string; the reset "
        "(reset_to_last_good) runs once, between every block's step 3 and any step 4. "
        "Clearing DEPLOY_HOLD is operator-only."
    )
    posts = ["\n".join(header)]
    for tag in sorted(stateful):
        role = stateful[tag]
        state = "FAILED" if any(split_kind(n)[0] == tag for n in failed) else "changed"
        info = [
            f"{marker} snapshot role {tag} ({state}), digest8 {role['digest8']}",
            f"Compare its classify output with: {comparison(found['digest'])}",
            "Snapshot prefix per claim:",
            *(
                f"- {c['name']} ({c['namespace']}): "
                f"{prefix(tag, role['digest8'], c['name'])}"
                for c in role["claims"]
            ),
        ]
        whole = "\n".join(info) + "\n" + block(tag, role, found["digest"])
        if len(whole) <= forge.DISCORD_MAX:
            posts.append(whole)
        else:
            # A role with many claims splits between its prefixes and its block, never inside
            # the block. A block still over the limit fails its send, so the run reads red.
            posts += [
                "\n".join(info),
                f"{marker} {tag}'s commands:\n" + block(tag, role, found["digest"]),
            ]
    return posts


def add_parser(commands) -> None:
    """Add the `hold` subcommand to `promote.py`'s subparsers."""
    cmd = commands.add_parser(
        "hold", help="post the stateful hold settle wrote to the deployer's channel"
    )
    cmd.add_argument("target", help="R, the held commit")
    cmd.add_argument("--hold", required=True, help="the DEPLOY_HOLD value settle wrote")
    cmd.add_argument(
        "--bundle-dir", type=Path, required=True, help="where decide wrote its record"
    )
    cmd.add_argument("--verdict", default="", help="verify's verdict")
    cmd.add_argument("--failed", default="", help="verify's failed names, comma-joined")


def run(args: argparse.Namespace, post: Callable[[str], None]) -> int:
    """`promote.py hold`: read the record, build the post, send each message.

    Returns FAILED when the record is unreadable or any send fails, so the run reads red.
    """
    if not _SHA.fullmatch(args.target) or not args.hold.startswith(
        f"{args.target} stateful="
    ):
        error(f"hold takes R's 40-hex SHA and its stateful hold, not {args.hold!r}")
        return USAGE_ERROR
    failed = args.failed.split(",") if _NAMES.fullmatch(args.failed) else []
    try:
        found = read(args.bundle_dir)
    except ValueError as err:
        error(f"hold: {err}")
        return FAILED
    posts = messages(args.target, args.hold, args.verdict, failed, found)
    for n, content in enumerate(posts, 1):
        try:
            post(content)
        except forge.ForgeError as err:
            error(f"hold: message {n} of {len(posts)} was not posted: {err}")
            return FAILED
    print(
        f"promote.py: posted the stateful hold for {args.target} in {len(posts)} messages"
    )
    return OK
