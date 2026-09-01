"""Which k8s changes the tick may deploy on its own: the diff-shape predicate and the split.

Auto-deploy is the highest-stakes decision in the deployer -- an ineligible role deployed
without a working gate is a change nobody watched land. A service qualifies only when the
push touched nothing but its `defaults/main.yml` and every changed line there is an image
pin; then two caps bound the batch, because every promoted service shares one playbook run
and one timeout, and a rollback resets the whole merged range.
"""

# ansible/roles/setup/gitops_deploy/files/test_deploy_k8s_eligibility.py

from deploy_changes import services_from_changed_paths
from deploy_k8s import is_image_only_diff, split_k8s_auto_deploy


# ── k8s auto-deploy: the diff-shape predicate ───────────────────────────────────────────────
_SPEEDTEST_DEFAULTS = "ansible/roles/k8s/speedtest/defaults/main.yml"


def _diff(*lines: str) -> str:
    """A unified diff for _SPEEDTEST_DEFAULTS carrying the given changed lines."""
    header = f"--- a/{_SPEEDTEST_DEFAULTS}\n+++ b/{_SPEEDTEST_DEFAULTS}\n@@ -2 +2 @@\n"
    return header + "".join(line + "\n" for line in lines)


def test_image_only_diff_accepts_a_pure_image_bump():
    assert is_image_only_diff(
        _diff(
            "-speedtest_k8s_image: openspeedtest/latest:v2.0.4",
            "+speedtest_k8s_image: openspeedtest/latest:v2.0.5",
        )
    )


def test_image_only_diff_rejects_a_bundled_non_image_line():
    assert not is_image_only_diff(
        _diff(
            "-speedtest_k8s_image: openspeedtest/latest:v2.0.4",
            "+speedtest_k8s_image: openspeedtest/latest:v2.0.5",
            "-speedtest_k8s_replicas: 1",
            "+speedtest_k8s_replicas: 2",
        )
    )


def test_image_only_diff_ignores_file_headers_not_content():
    # `--- a/...` / `+++ b/...` start with -/+ but are metadata, not changed lines.
    assert is_image_only_diff(
        _diff("-speedtest_k8s_image: a:1", "+speedtest_k8s_image: a:2")
    )


def test_image_only_diff_rejects_an_empty_diff():
    # Nothing to prove -> fail closed, so an unreadable/empty git diff defers.
    assert not is_image_only_diff("")


def test_image_only_diff_rejects_a_header_only_diff():
    assert not is_image_only_diff(
        f"--- a/{_SPEEDTEST_DEFAULTS}\n+++ b/{_SPEEDTEST_DEFAULTS}\n"
    )


def test_image_only_diff_rejects_a_commented_out_image_line():
    assert not is_image_only_diff(
        _diff("-# speedtest_k8s_image: a:1", "+# speedtest_k8s_image: a:2")
    )


def test_image_only_diff_accepts_a_digest_bump():
    # The 18 mutable-tag digest pins are the population the digest automerge rule targets.
    assert is_image_only_diff(
        _diff(
            "-littlelink_k8s_image: littlelink:latest@sha256:aaa",
            "+littlelink_k8s_image: littlelink:latest@sha256:bbb",
        )
    )


# ── k8s auto-deploy: the eligibility split ──────────────────────────────────────────────────
def _split(
    paths,
    *,
    denylist=frozenset(),
    pilot=frozenset(),
    enabled=True,
    image_only=True,
    max_per_tick=0,
    claim_services=(),
    max_claim_services_per_tick=0,
):
    cs = services_from_changed_paths(paths)
    return split_k8s_auto_deploy(
        cs,
        paths,
        denylist=denylist,
        pilot=pilot,
        enabled=enabled,
        image_only=lambda _svc: image_only,
        max_per_tick=max_per_tick,
        declares_claims=lambda svc: svc in set(claim_services),
        max_claim_services_per_tick=max_claim_services_per_tick,
    )


def test_split_k8s_promotes_an_image_only_bump():
    cs = _split([_SPEEDTEST_DEFAULTS], denylist={"traefik"})
    assert cs.k8s_deploy == {"speedtest"}
    assert cs.k8s == set()


def test_split_k8s_disabled_reproduces_todays_behaviour_exactly():
    cs = _split([_SPEEDTEST_DEFAULTS], denylist={"traefik"}, enabled=False)
    assert cs.k8s_deploy == set()
    assert cs.k8s == {"speedtest"}


def _defaults_for(svc):
    return f"ansible/roles/k8s/{svc}/defaults/main.yml"


def test_split_k8s_caps_how_many_services_one_tick_takes_on():
    # The promoted set shares ONE ansible-playbook run and one K8S_DEPLOY_TIMEOUT_S, and a
    # timeout git-resets the whole merged range — so an uncapped tick can discard four good
    # image bumps because the fifth failed to roll out.
    paths = [_defaults_for(s) for s in ("speedtest", "freshrss", "sonarr", "radarr")]
    cs = _split(paths, max_per_tick=2)
    assert len(cs.k8s_deploy) == 2
    # The surplus stays in cs.k8s, which defer-and-alerts — so it reaches the operator as a
    # Discord message naming the services to deploy by hand. It is NOT retried automatically:
    # the ff-merge precedes the deploy, so the next tick sees local == origin and noops. This
    # assertion covers the partition only; nothing here should be read as a retry guarantee.
    assert cs.k8s == {"speedtest", "freshrss", "sonarr", "radarr"} - cs.k8s_deploy


def test_split_k8s_cap_is_deterministic():
    # Same input, same promotion — otherwise which bumps land depends on set iteration order.
    paths = [_defaults_for(s) for s in ("speedtest", "freshrss", "sonarr", "radarr")]
    assert _split(paths, max_per_tick=2).k8s_deploy == (
        _split(paths, max_per_tick=2).k8s_deploy
    )


def test_split_k8s_cap_of_zero_promotes_everything_eligible():
    paths = [_defaults_for(s) for s in ("speedtest", "freshrss")]
    assert _split(paths, max_per_tick=0).k8s_deploy == {"speedtest", "freshrss"}


def test_split_k8s_never_promotes_a_denylisted_service():
    cs = _split([_SPEEDTEST_DEFAULTS], denylist={"speedtest"})
    assert cs.k8s_deploy == set()
    assert cs.k8s == {"speedtest"}


def test_split_k8s_rejects_a_non_image_diff():
    cs = _split([_SPEEDTEST_DEFAULTS], denylist={"traefik"}, image_only=False)
    assert cs.k8s_deploy == set()
    assert cs.k8s == {"speedtest"}


def test_split_k8s_blocks_a_service_with_a_second_changed_path():
    # Clean image bump, but the same push also edits the role's tasks/ — deploying would apply
    # an unsoaked structural change alongside it.
    cs = _split(
        [_SPEEDTEST_DEFAULTS, "ansible/roles/k8s/speedtest/tasks/main.yml"],
        denylist={"traefik"},
    )
    assert cs.k8s_deploy == set()
    assert cs.k8s == {"speedtest"}


def test_split_k8s_pilot_scope_restricts_eligibility():
    paths = [_SPEEDTEST_DEFAULTS, "ansible/roles/k8s/littlelink/defaults/main.yml"]
    cs = _split(paths, denylist={"traefik"}, pilot={"speedtest"})
    assert cs.k8s_deploy == {"speedtest"}
    assert cs.k8s == {"littlelink"}


def test_split_k8s_empty_pilot_means_the_denylist_governs():
    # Slice 3 (2026-08-16) cleared the pilot list. An empty pilot must mean "everything not
    # denylisted", never "nothing" — the opposite reading of the same falsy value, and the one
    # that would silently disarm the feature instead of widening it.
    paths = [_SPEEDTEST_DEFAULTS, _defaults_for("littlelink"), _defaults_for("sonarr")]
    cs = _split(paths, denylist={"sonarr"}, pilot=frozenset())
    assert cs.k8s_deploy == {"speedtest", "littlelink"}
    assert cs.k8s == {"sonarr"}


def test_split_k8s_denies_the_services_the_pilot_used_to_mask():
    # These six sat outside the denylist only because the pilot named neither them nor anything
    # else; each matches an exclusion class the design already publishes. Clearing the pilot
    # without adding them would have armed all six at once.
    masked = ("qbittorrent", "bazarr", "tdarr", "livesync", "valheim", "valheim-stats")
    cs = _split([_defaults_for(s) for s in masked], denylist=set(masked))
    assert cs.k8s_deploy == set()
    assert cs.k8s == set(masked)


def test_split_k8s_defers_when_the_tick_also_carries_docker_services():
    # main()'s k8s branch returns before the Docker deploy + health gate, so promoting here
    # would silently skip them. Defer instead.
    paths = [
        _SPEEDTEST_DEFAULTS,
        "ansible/roles/containers/dozzle/templates/docker-compose.yml.j2",
    ]
    cs = _split(paths, denylist={"traefik"})
    assert cs.k8s_deploy == set()
    assert cs.k8s == {"speedtest"}
    assert cs.services == {"dozzle"}


def test_split_k8s_combined_push_deploys_eligible_defers_denylisted():
    paths = [_SPEEDTEST_DEFAULTS, "ansible/roles/k8s/traefik/defaults/main.yml"]
    cs = _split(paths, denylist={"traefik"})
    assert cs.k8s_deploy == {"speedtest"}
    assert cs.k8s == {"traefik"}


# ── the claim-declaring cap (2026-08-22 review H2) ──────────────────────────────────────────
# Each claim-declaring service pays its own snapshot+revert phase SERIALLY inside the single
# rollback playbook run, while K8S_ROLLBACK_TIMEOUT_S is derived for the worst SINGLE one — so
# two co-batched already exceed it and killpg lands mid-revert, after volume-revert has scaled
# the workload to zero and attached its volume in maintenance mode. The budget arithmetic itself
# is pinned by ansible/tests/test_rollback_timeout_budget.py; these cover the partition.


def test_split_k8s_caps_claim_declaring_services_separately():
    paths = [_defaults_for(s) for s in ("radarr", "sonarr", "bazarr")]
    cs = _split(
        paths,
        claim_services=("radarr", "sonarr", "bazarr"),
        max_claim_services_per_tick=1,
    )
    assert len(cs.k8s_deploy) == 1
    assert cs.k8s == {"radarr", "sonarr", "bazarr"} - cs.k8s_deploy


def test_split_k8s_claim_cap_still_batches_claim_free_services():
    # Why a SEPARATE cap rather than max_per_tick=1: claim-free services cost nothing on the
    # revert path, so they must keep batching.
    paths = [_defaults_for(s) for s in ("radarr", "sonarr", "speedtest", "littlelink")]
    cs = _split(
        paths,
        claim_services=("radarr", "sonarr"),
        max_claim_services_per_tick=1,
        max_per_tick=3,
    )
    assert len([s for s in cs.k8s_deploy if s in ("radarr", "sonarr")]) == 1
    assert {"speedtest", "littlelink"} <= cs.k8s_deploy


def test_split_k8s_claim_cap_respects_max_per_tick_too():
    # Both caps bind; the claim cap must not become a way to exceed the batch cap.
    paths = [
        _defaults_for(s) for s in ("radarr", "speedtest", "littlelink", "freshrss")
    ]
    cs = _split(
        paths,
        claim_services=("radarr",),
        max_claim_services_per_tick=1,
        max_per_tick=2,
    )
    assert len(cs.k8s_deploy) == 2


def test_split_k8s_claim_cap_is_deterministic():
    paths = [_defaults_for(s) for s in ("radarr", "sonarr", "bazarr")]
    claims = ("radarr", "sonarr", "bazarr")
    first = _split(
        paths, claim_services=claims, max_claim_services_per_tick=1
    ).k8s_deploy
    second = _split(
        paths, claim_services=claims, max_claim_services_per_tick=1
    ).k8s_deploy
    assert first == second


def test_split_k8s_defers_the_surplus_when_every_promotable_declares_claims():
    # No claim-free service to fill the batch: promote exactly one, and the rest stay in cs.k8s,
    # which defer-and-alerts.
    paths = [_defaults_for(s) for s in ("radarr", "sonarr")]
    cs = _split(
        paths, claim_services=("radarr", "sonarr"), max_claim_services_per_tick=1
    )
    assert len(cs.k8s_deploy) == 1
    assert len(cs.k8s) == 1


def test_split_k8s_claim_cap_of_zero_leaves_the_old_behaviour():
    # 0 disables the claim cap; max_per_tick alone then governs, exactly as before this landed.
    paths = [_defaults_for(s) for s in ("radarr", "sonarr")]
    cs = _split(
        paths, claim_services=("radarr", "sonarr"), max_claim_services_per_tick=0
    )
    assert cs.k8s_deploy == {"radarr", "sonarr"}
