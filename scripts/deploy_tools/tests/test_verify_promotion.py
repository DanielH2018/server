"""`promote.py verify`: what it expects of T, and the verdict it prints, as clean/flagged pairs.

Each bundle is a file map in the shape `flux push artifact` publishes: `digests.json`, the
kustomize `flux/kustomization.yaml`, and one `flux/<name>.yaml` per Kustomization, written by
the renderer's own `tag_kustomization`. Statuses come from a fake read whose answer can move
with a fake clock, so a timeout costs no real time.

Run: uv run pytest scripts/deploy_tools/tests/test_verify_promotion.py
"""

import pytest
import yaml
from _promote_fakes import bundle_files
from renderer.renderer_lib.bundle import bundle_files as render_bundle
from renderer.renderer_lib.bundle import digests as render_digests
from renderer.renderer_lib.bundle_readme import BUNDLE_README
from renderer.renderer_lib.bundle import (
    digests_text,
    kustomization_text,
    tag_kustomization,
)

import forge
from deploy_tools.promote_lib import verify_promotion as vp

T = "a" * 40
PRIOR = "b" * 40


def ks(name: str, *depends: str, suspend: bool = False, timeout: str = "10m") -> str:
    """One Kustomization as the renderer writes a tag's, with `dependsOn` and `suspend`."""
    doc = tag_kustomization(name, substitutes=False, timeout=timeout)
    if depends:
        doc["spec"]["dependsOn"] = [{"name": d} for d in depends]
    if suspend:
        doc["spec"]["suspend"] = True
    return yaml.safe_dump(doc, sort_keys=False)


def digests(**footprints: str) -> dict:
    found = {
        name.replace("_", "-"): {"own": f"o-{v}", "footprint": v}
        for name, v in footprints.items()
    }
    found.setdefault("-unclassified", {"own": "u", "footprint": "u"})
    found["_unclassified"] = found.pop("-unclassified")
    return found


PROD = bundle_files(
    digests(bento_pdf="a", sonarr="s"), **{"bento-pdf": ks("bento-pdf")}
)
CHANGED = bundle_files(
    digests(bento_pdf="b", sonarr="s"),
    **{
        "bento-pdf": ks("bento-pdf"),
        "bento-pdf-deferred": ks("bento-pdf-deferred", "bento-pdf"),
        "sonarr": ks("sonarr"),
    },
)


class Clock:
    """A monotonic clock that `sleep` advances, so a wait costs no real time."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps = 0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.sleeps += 1


def reads(statuses: dict[str, dict[str, str]], clock: Clock | None = None):
    """Status reads that answer `statuses[commit]`, `{}` for a commit with none."""
    clock = clock or Clock()
    return vp.StatusReads(
        lambda sha, since: dict(statuses.get(sha, {})), clock.sleep, clock
    )


def ok(*names: str) -> dict[str, str]:
    return {f"flux/{n}": "success" for n in names}


# -- the verdict -----------------------------------------------------------------------------


def test_every_expected_status_success_is_good():
    expected = vp.expected_set(CHANGED, PROD)
    statuses = {T: ok("homelab", "bento-pdf", "bento-pdf-deferred")}
    assert vp.verify(reads(statuses), T, PRIOR, expected) == vp.Verdict("good")


def test_one_failure_is_failed_naming_it():
    expected = vp.expected_set(CHANGED, PROD)
    states = {**ok("homelab", "bento-pdf-deferred"), "flux/bento-pdf": "failure"}
    verdict = vp.verify(reads({T: states}), T, PRIOR, expected)
    assert (verdict.verdict, verdict.failed) == ("failed", ("bento-pdf",))


def test_an_error_status_is_failed_and_a_pending_one_keeps_waiting():
    expected = vp.expected_set(CHANGED, PROD)
    errored = {**ok("homelab", "bento-pdf-deferred"), "flux/bento-pdf": "error"}
    assert vp.verify(reads({T: errored}), T, PRIOR, expected).failed == ("bento-pdf",)
    clock = Clock()
    pending = {**ok("homelab", "bento-pdf-deferred"), "flux/bento-pdf": "pending"}
    verdict = vp.verify(reads({T: pending}, clock), T, PRIOR, expected)
    assert verdict.failed == ("bento-pdf",)
    assert clock.now >= expected.timeout_s, "a pending status waited out the timeout"


def test_a_timeout_names_each_expected_name_with_no_status():
    expected = vp.expected_set(CHANGED, PROD)
    verdict = vp.verify(reads({T: ok("homelab")}), T, PRIOR, expected)
    assert verdict == vp.Verdict("failed", ("bento-pdf", "bento-pdf-deferred"))


def test_an_empty_status_list_keeps_waiting_and_never_reads_good():
    """Failure class 3: an empty list is never success, however long it stays empty."""
    expected = vp.expected_set(PROD, PROD)
    clock = Clock()
    verdict = vp.verify(reads({}, clock), T, PRIOR, expected)
    assert verdict == vp.Verdict("failed", ("homelab",))
    assert clock.sleeps == expected.timeout_s // vp.POLL_S


def test_a_failed_status_read_is_retried_rather_than_read_as_good():
    expected = vp.expected_set(PROD, PROD)
    clock = Clock()
    answers = [forge.ForgeError("HTTP 502"), ok("homelab")]

    def statuses(sha, since):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    verdict = vp.verify(vp.StatusReads(statuses, clock.sleep, clock), T, "", expected)
    assert verdict == vp.Verdict("good") and clock.sleeps == 1


# -- what it expects -------------------------------------------------------------------------


def test_a_changed_tag_expects_itself_its_deferred_dependent_and_homelab():
    assert vp.expected_set(CHANGED, PROD).names == {
        "homelab",
        "bento-pdf",
        "bento-pdf-deferred",
    }


def test_an_unchanged_failing_tag_neither_holds_nor_blocks_last_good_is_clean():
    expected = vp.expected_set(CHANGED, PROD)
    states = {
        **ok("homelab", "bento-pdf", "bento-pdf-deferred"),
        "flux/sonarr": "failure",
    }
    assert vp.verify(reads({T: states}), T, PRIOR, expected) == vp.Verdict("good")


def test_an_unchanged_failing_tag_holds_once_every_name_is_expected_is_flagged():
    """The red half: a verify that expected every name would hold on sonarr's old failure."""
    widened = bundle_files(
        {**digests(bento_pdf="b", sonarr="s"), "_unclassified": {"footprint": "u2"}},
        **{name: ks(name) for name in ("bento-pdf", "sonarr")},
    )
    expected = vp.expected_set(widened, PROD)
    assert expected.widened
    states = {**ok("homelab", "bento-pdf"), "flux/sonarr": "failure"}
    verdict = vp.verify(reads({T: states}), T, PRIOR, expected)
    assert (verdict.verdict, verdict.failed) == ("failed", ("sonarr",))


def test_a_suspended_kustomization_is_not_expected():
    suspended = bundle_files(
        digests(bento_pdf="b", sonarr="s2"),
        **{"bento-pdf": ks("bento-pdf"), "sonarr": ks("sonarr", suspend=True)},
    )
    assert vp.expected_set(suspended, PROD).names == {"homelab", "bento-pdf"}


def _rendered(roles, value: str) -> dict[str, bytes]:
    """The renderer's own bundle of `sonarr` and its deferred directory, holding `value`."""
    configmap = f"kind: ConfigMap\nmetadata:\n  name: x\ndata:\n  v: {value}\n"
    directories = {
        "sonarr": {"x.yaml": configmap},
        "sonarr-deferred": {"x.yaml": configmap},
    }
    files = render_bundle(directories, {}, ["sonarr"], "10m", "x=ENC[y]\n", roles=roles)
    return {path: text.encode() for path, text in files.items()}


def _sonarr_role(tmp_path, defaults: str):
    (tmp_path / "sonarr" / "defaults").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sonarr" / "defaults" / "main.yml").write_text(defaults)
    return tmp_path


def test_a_suspended_tag_that_never_posts_reads_good_without_waiting(tmp_path):
    """D5.1b: a good verdict, so `settle` moves `last-good` and never resets.

    `soak` is expected, because the suspend moves its footprint, and it judges no service.
    """
    prod = _rendered(_sonarr_role(tmp_path / "prod", "---\n"), "1")
    target = _rendered(_sonarr_role(tmp_path / "t", "flux_suspend: true\n"), "2")
    expected = vp.expected_set(target, prod)
    assert expected.names == {"homelab", "soak"}
    clock = Clock()
    assert (
        vp.verify(reads({T: ok("homelab", "soak")}, clock), T, PRIOR, expected).verdict
        == "good"
    )
    assert clock.sleeps == 0


def test_the_same_tag_unsuspended_that_never_posts_times_out_and_fails(tmp_path):
    prod = _rendered(_sonarr_role(tmp_path / "prod", "---\n"), "1")
    target = _rendered(_sonarr_role(tmp_path / "t", "---\n"), "2")
    expected = vp.expected_set(target, prod)
    verdict = vp.verify(reads({T: ok("homelab")}), T, PRIOR, expected)
    assert verdict == vp.Verdict("failed", ("soak", "sonarr", "sonarr-deferred"))


@pytest.mark.parametrize("flag, names", [(True, set()), (False, {"sonarr-deferred"})])
def test_digests_suspended_leaves_out_every_kustomization_of_the_tag(flag, names):
    """The `suspended` field decides even for a document that carries no `spec.suspend`."""
    prints = digests(bento_pdf="a", sonarr="s2")
    prints["sonarr"]["suspended"] = flag
    target = bundle_files(
        prints,
        **{
            "bento-pdf": ks("bento-pdf"),
            "sonarr": ks("sonarr", suspend=True),
            "sonarr-deferred": ks("sonarr-deferred", "sonarr"),
        },
    )
    assert vp.expected_set(target, PROD).names == {"homelab"} | names


def test_an_unclassified_change_expects_every_name():
    files = bundle_files(
        {**digests(bento_pdf="a", sonarr="s"), "_unclassified": {"footprint": "u2"}},
        **{name: ks(name) for name in ("bento-pdf", "sonarr")},
    )
    assert vp.expected_set(files, PROD).names == {"homelab", "bento-pdf", "sonarr"}


def test_a_bundle_without_digests_json_expects_every_kustomization_it_holds():
    files = bundle_files(None, **{name: ks(name) for name in ("bento-pdf", "sonarr")})
    found = vp.expected_set(files, PROD)
    assert found.names == {"homelab", "bento-pdf", "sonarr"} and found.widened


def test_an_empty_changed_set_expects_homelab_alone():
    assert vp.expected_set(PROD, PROD).names == {"homelab"}


def test_a_shared_kind_adds_its_status_and_the_kustomize_root_adds_none():
    """`cluster-vars` is a shared kind with its own Kustomization; the root file is kustomize's."""
    files = bundle_files(
        {**digests(bento_pdf="a", sonarr="s"), "cluster-vars": {"footprint": "v2"}},
        **{"bento-pdf": ks("bento-pdf"), "cluster-vars": ks("cluster-vars")},
    )
    names = vp.expected_set(files, PROD).names
    assert names == {"homelab", "cluster-vars"}
    assert "kustomization" not in names


def test_the_first_bundle_carrying_cluster_vars_expects_its_status():
    """D3.3a's first promotion, from the renderer's own files: D2's empty bundle to the
    `--shared-only` bundle. The new name is a move, not a widening."""
    prod = {
        "flux/kustomization.yaml": kustomization_text([]).encode(),
        "digests.json": digests_text(
            render_digests({"README.md": BUNDLE_README.encode()}, [], kinds=())
        ).encode(),
    }
    target = {
        path: text.encode()
        for path, text in render_bundle({}, {}, [], "10m", "x=ENC[y]\n").items()
    }
    expected = vp.expected_set(target, prod)
    assert (expected.names, expected.widened) == (
        {"homelab", "cluster-vars", "soak"},
        "",
    )


# -- a dependent that was already failing (H1.1i) --------------------------------------------

# A node outage: `longhorn` fails on every revision. `longhorn-backup` and `longhorn-reconcile`
# depend on `longhorn`, which depends on `cluster-base`. `bento-pdf` and `metallb` stand apart.
OUTAGE_GRAPH = {
    "cluster-base": ks("cluster-base"),
    "longhorn": ks("longhorn", "cluster-base"),
    "longhorn-backup": ks("longhorn-backup", "longhorn"),
    "longhorn-reconcile": ks("longhorn-reconcile", "longhorn"),
    "metallb": ks("metallb"),
    "bento-pdf": ks("bento-pdf"),
}
OUTAGE_NAMES = ("cluster_base", "longhorn", "longhorn_backup", "longhorn_reconcile")


def outage(**moved: str) -> dict[str, bytes]:
    """The outage bundle, each footprint `a` unless `moved` names another."""
    prints = {n: "a" for n in (*OUTAGE_NAMES, "metallb", "bento_pdf")} | moved
    return bundle_files(digests(**prints), **OUTAGE_GRAPH)


OUTAGE_PROD = outage()
# T changes only cluster-base's footprint.
CLUSTER_BASE_T = outage(cluster_base="b")
EXPECTED_ELSEWHERE = ok(
    "homelab", "cluster-base", "longhorn-backup", "longhorn-reconcile"
)


def test_a_cluster_base_change_expects_longhorn_and_its_dependents_as_dependents():
    expected = vp.expected_set(CLUSTER_BASE_T, OUTAGE_PROD)
    assert expected.names == {
        "homelab",
        "cluster-base",
        "longhorn",
        "longhorn-backup",
        "longhorn-reconcile",
    }
    assert expected.dependents == {"longhorn", "longhorn-backup", "longhorn-reconcile"}


def test_a_dependent_that_read_failure_on_prods_commit_is_excused_is_clean(capsys):
    expected = vp.expected_set(CLUSTER_BASE_T, OUTAGE_PROD)
    states = {**EXPECTED_ELSEWHERE, "flux/longhorn": "failure"}
    verdict = vp.verify(
        reads({T: states}), T, PRIOR, expected, recorded={"longhorn": "failure"}
    )
    assert verdict == vp.Verdict("good", excused=("longhorn",))
    assert "excused longhorn" in capsys.readouterr().out


@pytest.mark.parametrize(
    "recorded",
    [{"longhorn": "success"}, {"longhorn": "error"}, {}],
    ids=["success-before", "error-before", "no-record"],
)
def test_a_dependent_not_recorded_as_failure_still_holds_is_flagged(recorded):
    """The red half: only a recorded `failure` excuses, so T may have broken the rest."""
    expected = vp.expected_set(CLUSTER_BASE_T, OUTAGE_PROD)
    states = {**EXPECTED_ELSEWHERE, "flux/longhorn": "failure"}
    verdict = vp.verify(reads({T: states}), T, PRIOR, expected, recorded=recorded)
    assert verdict == vp.Verdict("failed", ("longhorn",))


def test_a_name_whose_footprint_t_changed_is_never_excused():
    expected = vp.expected_set(outage(cluster_base="b", longhorn="b"), OUTAGE_PROD)
    states = {**EXPECTED_ELSEWHERE, "flux/longhorn": "failure"}
    verdict = vp.verify(
        reads({T: states}), T, PRIOR, expected, recorded={"longhorn": "failure"}
    )
    assert verdict == vp.Verdict("failed", ("longhorn",))


def test_a_dependent_of_an_excused_name_with_no_status_times_out_and_holds():
    """The timeout excuse is gone: `longhorn-reconcile` reaches `longhorn` and still counts."""
    expected = vp.expected_set(CLUSTER_BASE_T, OUTAGE_PROD)
    states = {
        **ok("homelab", "cluster-base", "longhorn-backup"),
        "flux/longhorn": "failure",
    }
    clock = Clock()
    verdict = vp.verify(
        reads({T: states}, clock), T, PRIOR, expected, recorded={"longhorn": "failure"}
    )
    assert verdict == vp.Verdict("failed", ("longhorn-reconcile",), ("longhorn",))
    assert clock.now >= expected.timeout_s


def test_an_outage_does_not_hold_a_revision_that_changes_neither_tag():
    expected = vp.expected_set(outage(bento_pdf="b"), OUTAGE_PROD)
    assert expected.names == {"homelab", "bento-pdf"}
    states = {
        **ok("homelab", "bento-pdf"),
        "flux/longhorn": "failure",
        "flux/metallb": "failure",
    }
    assert vp.verify(reads({T: states}), T, PRIOR, expected) == vp.Verdict("good")


def test_decides_record_holds_each_changed_name_with_a_status_on_prods_commit():
    prior = {
        **ok("cluster-base", "metallb"),
        "flux/longhorn": "failure",
        "flux/homelab": "success",
    }
    recorded = vp.prior_statuses(CLUSTER_BASE_T, OUTAGE_PROD, lambda: prior)
    assert recorded == {"cluster-base": "success", "longhorn": "failure"}


def test_decide_reads_no_status_when_the_changed_set_holds_no_dependent():
    def read():
        raise AssertionError("no dependent can be excused, so nothing is read")

    assert vp.prior_statuses(outage(bento_pdf="b"), OUTAGE_PROD, read) == {}


# -- the second document in a tag's file (D4.3) ----------------------------------------------

# bento-pdf's file holding `bento-pdf` and `bento-pdf-deferred`, as the renderer writes a tag
# that defers files: one `flux/<tag>.yaml`, two documents.
TWO_DOCUMENTS = bundle_files(
    digests(bento_pdf="b", sonarr="s"),
    **{
        "bento-pdf": ks("bento-pdf") + "---\n" + ks("bento-pdf-deferred", "bento-pdf"),
        "sonarr": ks("sonarr"),
    },
)


def test_a_tag_file_holding_two_documents_expects_both_and_passes_when_both_succeed():
    expected = vp.expected_set(TWO_DOCUMENTS, PROD)
    assert expected.names == {"homelab", "bento-pdf", "bento-pdf-deferred"}
    states = ok("homelab", "bento-pdf", "bento-pdf-deferred")
    assert vp.verify(reads({T: states}), T, PRIOR, expected) == vp.Verdict("good")


def test_a_missing_deferred_status_keeps_waiting_and_a_failure_holds():
    """The red proof that the expected set reads the second document in a file."""
    expected = vp.expected_set(TWO_DOCUMENTS, PROD)
    clock = Clock()
    waited = vp.verify(reads({T: ok("homelab", "bento-pdf")}, clock), T, "", expected)
    assert waited == vp.Verdict("failed", ("bento-pdf-deferred",))
    assert clock.now >= expected.timeout_s
    states = {**ok("homelab", "bento-pdf"), "flux/bento-pdf-deferred": "failure"}
    held = vp.verify(reads({T: states}), T, "", expected)
    assert held == vp.Verdict("failed", ("bento-pdf-deferred",))


# -- the handoff and the CLI -----------------------------------------------------------------


def test_the_handoff_carries_digests_and_flux_files_and_nothing_else(tmp_path):
    vp.write_handoff(tmp_path, {**PROD, "cluster/bento-pdf/x.yaml": b"x"}, CHANGED)
    prod, target = vp.read_handoff(tmp_path)
    assert prod == PROD and target == CHANGED


def test_the_handoff_carries_the_record_beside_the_bundles(tmp_path):
    vp.write_handoff(tmp_path, PROD, CHANGED, {"longhorn": "failure"})
    assert vp.read_handoff(tmp_path) == (PROD, CHANGED)
    assert vp.read_recorded(tmp_path) == {"longhorn": "failure"}
    (tmp_path / vp.RECORDED_FILE).unlink()
    assert vp.read_recorded(tmp_path) == {}, "no record excuses nothing"


def test_a_first_run_hands_on_no_prod(tmp_path):
    vp.write_handoff(tmp_path, None, CHANGED)
    assert vp.read_handoff(tmp_path) == (None, CHANGED)
    assert vp.expected_set(CHANGED, None).widened


def test_verify_without_a_handoff_exits_1_with_no_verdict(tmp_path, capsys):
    import argparse

    args = argparse.Namespace(
        target=T, prior=PRIOR, bundle_dir=tmp_path, moved_at="2026-10-15T01:00:00Z"
    )
    output = tmp_path / "out"
    assert vp.run(forge.connect({}), args, str(output)) == 1
    assert not output.exists(), "settle reads no verdict, and holds as unverified"
    assert "holds no target/" in capsys.readouterr().err


def test_a_good_verdict_exits_0_and_writes_the_outputs(tmp_path):
    output = tmp_path / "out"
    assert vp.report(T, vp.Verdict("good"), str(output)) == 0
    assert output.read_text() == "verdict=good\nfailed=\n"


def test_a_failed_verdict_exits_1_after_writing_the_outputs(tmp_path):
    output = tmp_path / "out"
    assert vp.report(T, vp.Verdict("failed", ("a", "b")), str(output)) == 1
    assert output.read_text() == "verdict=failed\nfailed=a,b\n"


def test_verify_without_a_zoned_move_time_is_a_usage_error(tmp_path, capsys):
    import argparse

    vp.write_handoff(tmp_path, PROD, PROD)
    args = argparse.Namespace(
        target=T, prior=PRIOR, bundle_dir=tmp_path, moved_at="2026-10-15 01:00"
    )
    assert vp.run(forge.connect({}), args, None) == 64
    assert "--moved-at" in capsys.readouterr().err
