"""`case_groups.isolate`: two concurrent groups share no scratch file, no debs label and no failure."""

from pathlib import Path
from types import SimpleNamespace

from validate.validate_lib import case_groups


def _prepared(tmp_path: Path) -> SimpleNamespace:
    debs = tmp_path / "work" / "debs"
    debs.mkdir(parents=True)
    (debs / "real-pi.deb").write_bytes(b"real")
    return SimpleNamespace(
        work=tmp_path / "work",
        debs={"pi-head": debs / "real-pi.deb"},
        versions={"pi-head": "1.0"},
        failed=[],
        image="homelab-host-test:24.04",
    )


def test_a_groups_seal_and_failure_stay_out_of_the_other_group(tmp_path):
    cases = _prepared(tmp_path)
    sealing = case_groups.isolate(cases, "by_hand")
    other = case_groups.isolate(cases, "bootstrap_repo_case")

    sealed = sealing.work / "debs" / "sealed-pi.deb"
    sealed.write_bytes(b"sealed")
    sealing.debs["pi-head"] = sealed
    sealing.versions["pi-head"] = "1.0.sealed"
    (sealing.work / "repo" / "InRelease").write_text("by_hand's index")
    (sealing.work / "debs" / "real-pi.deb").write_bytes(b"overwritten")
    sealing.failed.append("by_hand")

    assert {
        "other_pi_head": other.debs["pi-head"].read_bytes(),
        "other_version": other.versions["pi-head"],
        "other_repo": sorted(p.name for p in (other.work / "repo").iterdir()),
        "prepared_deb": (cases.work / "debs" / "real-pi.deb").read_bytes(),
        "failed": (cases.failed, other.failed),
        "image": other.image,
    } == {
        "other_pi_head": b"real",
        "other_version": "1.0",
        "other_repo": [],
        "prepared_deb": b"real",
        "failed": ([], []),
        "image": "homelab-host-test:24.04",
    }


def test_a_group_that_raises_is_recorded_and_its_output_is_one_block(tmp_path, capsys):
    cases = _prepared(tmp_path)

    def broken(group) -> None:
        print("broken starts", file=case_groups.sys.stderr)
        raise ValueError("no such deb")

    def clean(group) -> None:
        print("clean ran", file=case_groups.sys.stderr)

    case_groups.run_groups(cases, [("broken", broken), ("clean", clean)], jobs=2)

    err = capsys.readouterr().err
    broken_block = err[err.index("broken starts") : err.index("[DONE] broken")]
    assert {
        "failed": cases.failed,
        "traceback_in_block": "ValueError: no such deb" in broken_block,
        "clean_outside_block": "clean ran" not in broken_block,
    } == {
        "failed": ["broken: raised ValueError"],
        "traceback_in_block": True,
        "clean_outside_block": True,
    }
