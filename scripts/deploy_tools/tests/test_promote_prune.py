"""`promote.py prune`'s selection, as `_is_pruned` and `_is_kept` pairs, and its CLI.

Each `_is_kept` case is a `_is_pruned` case with one property changed, so a selection that
stopped telling them apart fails one half. The selection tests call the pure
`artifact_retention.select_prunable`. The CLI tests run `promote.py prune` against a real
throwaway master and the real forge clients over a fake transport.

Run: uv run pytest scripts/deploy_tools/tests/test_promote_prune.py
"""

from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import pytest

import artifact_retention
import forge
import promote
from _promote_fakes import World, digest_of, package_version

NOW = datetime(2026, 11, 1, tzinfo=UTC)
PROD = "c" * 40
ANCESTOR = "a" * 40
DESCENDANT = "d" * 40
PROD_FACT = (PROD, digest_of("prod"))
LISTING = urlsplit(forge.PACKAGE_VERSIONS_URL).path


def version(name: str, *tags: str, age_days: int = 30) -> artifact_retention.Version:
    """A version whose digest is `digest_of(name)`, pushed `age_days` before NOW."""
    created = NOW - timedelta(days=age_days)
    version_id = int(digest_of(name)[7:15], 16)
    return artifact_retention.Version(
        version_id, digest_of(name), frozenset(tags), created
    )


def index_tag(name: str) -> str:
    """The tag cosign's referrers fallback gives the index of `digest_of(name)`'s signatures."""
    return digest_of(name).replace(":", "-")


def select(
    versions,
    *,
    subjects=None,
    ancestry=None,
    prod=PROD_FACT,
    last_good=None,
) -> set[str]:
    """The digests `select_prunable` deletes, with the `prod` version listed beside `versions`."""
    listed = [version("prod", f"sha-{PROD}", "prod"), *versions]
    facts = artifact_retention.PruneFacts(
        versions=listed,
        prod=prod,
        last_good=last_good,
        ancestry={PROD: True, ANCESTOR: True, DESCENDANT: False} | (ancestry or {}),
        subjects=subjects or {},
        now=NOW,
    )
    return {v.digest for v, _ in artifact_retention.select_prunable(facts).delete}


# The smallest listing that prunes something of each kind: a strict ancestor's version, its
# cosign bundle, its referrers-fallback index, and a bundle of a digest no longer listed.
BASE = [
    version("ancestor", f"sha-{ANCESTOR}"),
    version("ancestor-bundle"),
    version("ancestor-index", index_tag("ancestor")),
]
BASE_SUBJECTS = {digest_of("ancestor-bundle"): digest_of("ancestor")}


def test_a_strict_ancestors_version_its_bundle_and_its_index_are_pruned():
    assert select(BASE, subjects=BASE_SUBJECTS) == {
        digest_of("ancestor"),
        digest_of("ancestor-bundle"),
        digest_of("ancestor-index"),
    }


def test_a_sha_version_whose_commit_descends_from_prod_is_kept():
    versions = [version("descendant", f"sha-{DESCENDANT}")]
    assert select(versions) == set()


def test_prods_own_sha_version_is_kept_without_its_prod_tag():
    """`merge-base --is-ancestor` answers yes for prod's own commit, so strictness keeps it."""
    versions = [version("prod-commit", f"sha-{PROD}")]
    assert select(versions) == set()


def test_an_untagged_version_past_14_days_with_no_subject_is_pruned():
    subjects = {digest_of("drill"): None}
    assert select([version("drill")], subjects=subjects) == {digest_of("drill")}


def test_a_version_13_days_old_is_kept():
    versions = [
        version("ancestor", f"sha-{ANCESTOR}", age_days=13),
        version("drill", age_days=13),
    ]
    assert select(versions, subjects={digest_of("drill"): None}) == set()


def test_a_bundle_whose_subject_the_listing_no_longer_holds_is_pruned():
    subjects = {digest_of("bundle"): digest_of("long-gone")}
    assert select([version("bundle")], subjects=subjects) == {digest_of("bundle")}


def test_a_bundle_of_prods_digest_is_kept():
    subjects = {digest_of("bundle"): digest_of("prod")}
    assert select([version("bundle")], subjects=subjects) == set()


def test_a_bundle_of_last_goods_digest_is_kept():
    versions = [version("last-good", "last-good"), version("bundle")]
    subjects = {digest_of("bundle"): digest_of("last-good")}
    assert (
        select(versions, subjects=subjects, last_good=digest_of("last-good")) == set()
    )


def test_the_index_of_prods_digest_is_kept():
    """`sha256-<hex>` never parses as `sha-<C>`, and prod's digest survives."""
    versions = [version("prod-index", index_tag("prod"))]
    assert select(versions) == set()


@pytest.mark.parametrize("tag", ["prod", "last-good", "verify-drill"])
def test_a_strict_ancestors_version_carrying_a_kept_tag_is_kept(tag):
    held = version(tag, f"sha-{ANCESTOR}", tag)
    prod = (PROD, held.digest) if tag == "prod" else PROD_FACT
    last_good = held.digest if tag == "last-good" else None
    assert select([held], prod=prod, last_good=last_good) == set()


@pytest.mark.parametrize(
    "broken",
    [
        {"prod": None},
        {"prod": artifact_retention.Failed("GHCR answered 500")},
        {"last_good": artifact_retention.Failed("GHCR answered 500")},
        {
            "subjects": {
                digest_of("ancestor-bundle"): artifact_retention.Failed("timed out")
            }
        },
        {"subjects": {}},
        {"ancestry": {ANCESTOR: artifact_retention.Failed("exit 128")}},
        {"prod": (PROD, digest_of("not-listed"))},
        {"last_good": digest_of("not-listed")},
    ],
    ids=[
        "prod-absent",
        "prod-read-fails",
        "last-good-read-fails",
        "one-subject-read-fails",
        "a-subject-unread",
        "one-ancestry-read-fails",
        "listing-misses-prod",
        "listing-misses-last-good",
    ],
)
def test_every_version_is_kept_when_a_read_fails_or_prod_is_absent(broken):
    assert select(BASE, **({"subjects": BASE_SUBJECTS} | broken)) == set()


def test_every_version_is_kept_when_the_listing_fails():
    facts = artifact_retention.PruneFacts(
        artifact_retention.Failed("HTTP 403"), PROD_FACT, None, {}, {}, NOW
    )
    assert artifact_retention.select_prunable(facts).failed


def test_a_signature_is_deleted_after_the_version_it_signs():
    facts = artifact_retention.PruneFacts(
        versions=[version("prod", "prod"), *BASE],
        prod=PROD_FACT,
        last_good=None,
        ancestry={ANCESTOR: True},
        subjects=BASE_SUBJECTS,
        now=NOW,
    )
    order = [v.digest for v, _ in artifact_retention.select_prunable(facts).delete]
    assert order[0] == digest_of("ancestor")


# -- the CLI ---------------------------------------------------------------------------------


def _stamp(days: int) -> str:
    return (NOW - timedelta(days=days)).isoformat()


@pytest.fixture
def world(tmp_path):
    """Master c0..c3 with `prod` at c2, listed two versions a page with prod's on page two."""
    w = World(tmp_path)
    c0, _, c2, c3 = w.commits
    w.set_prod(c2)
    bundle = digest_of("c0-bundle")
    w.transport.manifests[bundle] = {"subject": {"digest": digest_of(c0)}}
    w.transport.versions = [
        package_version(4, digest_of(c3), [f"sha-{c3}"], _stamp(30)),
        package_version(3, bundle, [], _stamp(30)),
        package_version(2, digest_of(c2), [f"sha-{c2}", "prod"], _stamp(30)),
        package_version(1, digest_of(c0), [f"sha-{c0}"], _stamp(30)),
    ]
    return w


def _prune(world: World, capsys) -> tuple[int, list[str], str]:
    argv = ["prune", "--repo", str(world.repo)]
    clients = world.clients()
    github = forge.GitHub(
        world.transport, "job-token", run=world.run, versions_per_page=2
    )
    clients = forge.Forge(clients.registry, github, clients.cosign)
    code = promote.main(argv, f=clients, now=NOW)
    out = capsys.readouterr()
    return code, out.out.splitlines(), out.err


def test_prune_deletes_the_selection_then_verifies_prod_is_clean(world, capsys):
    code, lines, _ = _prune(world, capsys)
    assert code == 0
    assert world.transport.deleted == [1, 3]
    assert world.run.verified == [digest_of(world.commits[2])]
    assert lines[-1] == (
        "listed=4 untagged_with_subject=1 selected=2 deleted=2 verified=prod"
    )


def test_a_refused_delete_stops_the_run_and_is_flagged(world, capsys):
    world.transport.api_forced[f"{LISTING}/1"] = 403
    code, _, err = _prune(world, capsys)
    assert code == 1
    assert world.transport.deleted == []
    assert "delete package version 1: HTTP 403" in err
    assert world.run.verified == [digest_of(world.commits[2])]


def test_a_prod_that_no_longer_verifies_is_flagged(world, capsys):
    world.run.cosign_fails = True
    code, _, err = _prune(world, capsys)
    assert code == 1
    assert "prod does not verify" in err


def test_a_listing_that_fails_deletes_nothing_and_is_flagged(world, capsys):
    world.transport.api_forced[LISTING] = 403
    code, _, err = _prune(world, capsys)
    assert code == 1
    assert world.transport.deleted == []
    assert "package versions page 1: HTTP 403" in err
