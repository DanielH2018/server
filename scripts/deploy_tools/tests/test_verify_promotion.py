"""`promote.py verify`: what it expects of T, and the verdict it prints, as clean/flagged pairs.

Each bundle is a file map in the shape `flux push artifact` publishes: `digests.json`, the
kustomize `flux/kustomization.yaml`, and one `flux/<name>.yaml` per Kustomization, written by
the renderer's own `tag_kustomization`. Statuses come from a fake read whose answer can move
with a fake clock, so a timeout costs no real time.

Run: uv run pytest scripts/deploy_tools/tests/test_verify_promotion.py
"""

import re

import pytest
import yaml
from _promote_fakes import bundle_files
from renderer.renderer_lib.bundle import tag_kustomization

import forge
from deploy_tools.promote_lib import verify as vp
from lib import yaml_fast
from lib.repo_paths import REPO

T = "a" * 40
PRIOR = "b" * 40
SYNC = REPO / "ansible/roles/k8s/flux/templates/sync.yaml.j2"


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


def test_a_dependent_already_failing_on_prods_commit_is_reported_not_expected():
    expected = vp.expected_set(CHANGED, PROD)
    statuses = {
        PRIOR: {"flux/bento-pdf-deferred": "failure"},
        T: ok("homelab", "bento-pdf"),
    }
    verdict = vp.verify(reads(statuses), T, PRIOR, expected)
    assert verdict == vp.Verdict("good", reported=("bento-pdf-deferred",))


def test_a_dependent_passing_on_prods_commit_stays_expected():
    expected = vp.expected_set(CHANGED, PROD)
    statuses = {PRIOR: ok("bento-pdf-deferred"), T: ok("homelab", "bento-pdf")}
    verdict = vp.verify(reads(statuses), T, PRIOR, expected)
    assert verdict == vp.Verdict("failed", ("bento-pdf-deferred",))


# -- the timeout -----------------------------------------------------------------------------


def test_the_timeout_is_the_largest_timeout_plus_the_interval_plus_the_largest_retry():
    """bento-pdf's Kustomization at the manifests role's 10m rollout timeout: 10m + 1m + 2m."""
    assert vp.expected_set(CHANGED, PROD).timeout_s == 10 * 60 + 60 + 2 * 60


def test_an_empty_bundle_times_out_at_the_roots_floor():
    assert (
        vp.promote_timeout([])
        == vp.ROOT_TIMEOUT_S + vp.OCI_INTERVAL_S + vp.ROOT_RETRY_S
    )


def test_an_unreadable_duration_is_an_error():
    with pytest.raises(ValueError, match="not a duration"):
        vp.promote_timeout([{"spec": {"timeout": "ten minutes"}}])


def _sync_docs() -> list[dict]:
    """The flux role's sync template with its Jinja removed, parsed."""
    text = re.sub(r"\{#.*?#\}", "", SYNC.read_text(), flags=re.S)
    text = re.sub(r"^\s*\{%.*%\}\s*$", "", text, flags=re.M)
    text = re.sub(r"\{\{.*?\}\}", "x", text)
    return [d for d in yaml_fast.safe_load_all(text) if isinstance(d, dict)]


def test_the_floor_matches_the_flux_roles_sync_template():
    docs = {(d["kind"], d["metadata"]["name"]): d["spec"] for d in _sync_docs()}
    root = docs[("Kustomization", "homelab")]
    assert (
        vp.seconds(docs[("OCIRepository", "homelab")]["interval"]) == vp.OCI_INTERVAL_S
    )
    assert vp.seconds(root["timeout"]) == vp.ROOT_TIMEOUT_S
    assert vp.seconds(root["retryInterval"]) == vp.ROOT_RETRY_S


# -- the handoff and the CLI -----------------------------------------------------------------


def test_the_handoff_carries_digests_and_flux_files_and_nothing_else(tmp_path):
    vp.write_handoff(tmp_path, {**PROD, "cluster/bento-pdf/x.yaml": b"x"}, CHANGED)
    prod, target = vp.read_handoff(tmp_path)
    assert prod == PROD and target == CHANGED


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
